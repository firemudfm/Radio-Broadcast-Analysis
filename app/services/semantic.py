from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from ..config import Settings
from ..s3_utils import is_allowed_audio_key, parse_s3_uri
from .conversation import ConversationService, find_normalized_span
from .llm import LocalLlmClient

logger = logging.getLogger(__name__)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


class SemanticDiscoveryService:
    """Discover cross-language concept mentions from full chunk transcripts.

    Exact brand/person/product matching is performed locally before any LLM
    call. Cross-language semantic verification is opt-in per keyword. Each
    complete transcript group is scanned by one shared worker for all campaigns.
    A short settle delay also makes this compatible with an already-installed
    Step 4C worker without creating duplicate mentions.
    """

    def __init__(
        self,
        settings: Settings,
        database: Any,
        s3_client: Any,
        station_service: Any,
        conversation_service: ConversationService,
        llm_client: LocalLlmClient,
    ) -> None:
        self._settings = settings
        self._database = database
        self._s3 = s3_client
        self._stations = station_service
        self._conversation = conversation_service
        self._llm = llm_client

    def scan_once(self) -> dict[str, int]:
        stats = {
            "groups_seen": 0,
            "groups_processed": 0,
            "keywords_checked": 0,
            "matches_created": 0,
            "exact_matches": 0,
            "semantic_matches": 0,
            "skipped_existing": 0,
            "errors": 0,
        }
        if not self._settings.RADIO_SEMANTIC_DISCOVERY_ENABLED:
            return stats
        bindings = self._database.active_bindings()
        if not bindings:
            return stats
        station_map = self._stations.station_map()
        revision = self._database.campaign_revision()
        groups = self._transcript_groups()
        stats["groups_seen"] = len(groups)

        processed_groups = 0
        for group in groups:
            if processed_groups >= self._settings.RADIO_SEMANTIC_GROUPS_PER_CYCLE:
                break
            station_id = group["station_id"]
            applicable = [
                binding
                for binding in bindings
                if station_id in binding.get("station_ids", [])
            ]
            if not applicable:
                continue
            pending = [
                binding
                for binding in applicable
                if not self._database.semantic_scan_is_current(
                    campaign_keyword_id=str(binding["keyword_id"]),
                    transcript_group_key=str(group["group_key"]),
                    source_fingerprint=str(group["fingerprint"]),
                    campaign_revision=revision,
                )
            ][: self._settings.RADIO_SEMANTIC_KEYWORDS_PER_GROUP]
            if not pending:
                continue
            processed_groups += 1
            stats["groups_processed"] += 1
            try:
                conversation = self._conversation.build(
                    {
                        "transcript_s3_key": group["keys"][0],
                        "conversation_scope": "source_group",
                        "context": "",
                        "display_name": "",
                    }
                )
            except Exception as error:
                logger.exception("Unable to assemble semantic transcript group %s", group["group_key"])
                for binding in pending:
                    self._record_error(binding, group, revision, error)
                    stats["errors"] += 1
                continue

            full_transcript = str(conversation.get("full_transcript") or "").strip()
            if not full_transcript:
                for binding in pending:
                    self._database.record_semantic_scan(
                        campaign_keyword_id=str(binding["keyword_id"]),
                        transcript_group_key=str(group["group_key"]),
                        source_fingerprint=str(group["fingerprint"]),
                        campaign_revision=revision,
                        status="not_matched",
                    )
                continue
            group_start = self._conversation_start(conversation)
            for binding in pending:
                stats["keywords_checked"] += 1
                monitor_from = _datetime(binding.get("monitor_from_utc"))
                if monitor_from and group_start and group_start < monitor_from:
                    self._database.record_semantic_scan(
                        campaign_keyword_id=str(binding["keyword_id"]),
                        transcript_group_key=str(group["group_key"]),
                        source_fingerprint=str(group["fingerprint"]),
                        campaign_revision=revision,
                        status="not_matched",
                    )
                    continue
                # If another compatible pipeline already materialized this
                # keyword/transcript pair, do not create a duplicate.
                if any(
                    self._database.mention_exists_for_keyword_transcript(
                        campaign_keyword_id=str(binding["keyword_id"]),
                        transcript_s3_key=key,
                    )
                    for key in group["keys"]
                ):
                    self._database.record_semantic_scan(
                        campaign_keyword_id=str(binding["keyword_id"]),
                        transcript_group_key=str(group["group_key"]),
                        source_fingerprint=str(group["fingerprint"]),
                        campaign_revision=revision,
                        status="not_matched",
                    )
                    stats["skipped_existing"] += 1
                    continue
                try:
                    result = self._exact_match(full_transcript, binding)
                    match_origin = "exact"
                    if result is None and bool(binding.get("semantic_matching")):
                        result = self._llm.match_keyword(
                            target=str(binding["display_name"]),
                            aliases=[str(value) for value in binding.get("aliases", [])],
                            keyword_type=str(binding.get("keyword_type") or "brand"),
                            detected_language=self._dominant_language(conversation),
                            full_transcript=full_transcript,
                            threshold=float(
                                binding.get("semantic_threshold")
                                or self._settings.RADIO_SEMANTIC_DEFAULT_THRESHOLD
                            ),
                        )
                        match_origin = "semantic"
                    if result is None or not result.get("is_match"):
                        self._database.record_semantic_scan(
                            campaign_keyword_id=str(binding["keyword_id"]),
                            transcript_group_key=str(group["group_key"]),
                            source_fingerprint=str(group["fingerprint"]),
                            campaign_revision=revision,
                            status="not_matched",
                        )
                        continue
                    record, audit = self._record_for_match(
                        binding=binding,
                        group=group,
                        station=station_map.get(station_id),
                        conversation=conversation,
                        result=result,
                    )
                    audit_key = self._audit_key(group, binding)
                    audit["audit_s3_key"] = audit_key
                    self._s3.put_object(
                        Bucket=self._settings.RADIO_S3_BUCKET,
                        Key=audit_key,
                        Body=json.dumps(audit, ensure_ascii=False, indent=2).encode("utf-8"),
                        ContentType="application/json; charset=utf-8",
                        ServerSideEncryption="AES256",
                    )
                    record["source_result_s3_key"] = audit_key
                    self._database.upsert_mention(record)
                    self._database.record_semantic_scan(
                        campaign_keyword_id=str(binding["keyword_id"]),
                        transcript_group_key=str(group["group_key"]),
                        source_fingerprint=str(group["fingerprint"]),
                        campaign_revision=revision,
                        status="matched",
                    )
                    stats["matches_created"] += 1
                    stats["exact_matches" if match_origin == "exact" else "semantic_matches"] += 1
                except Exception as error:
                    logger.exception(
                        "Semantic discovery failed group=%s keyword=%s",
                        group["group_key"],
                        binding["display_name"],
                    )
                    self._record_error(binding, group, revision, error)
                    stats["errors"] += 1
        return stats

    def _transcript_groups(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self._settings.RADIO_SEMANTIC_SCAN_LOOKBACK_DAYS
        )
        settled_before = datetime.now(timezone.utc) - timedelta(
            seconds=self._settings.RADIO_SEMANTIC_SETTLE_SECONDS
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self._settings.RADIO_S3_BUCKET,
            Prefix=self._settings.RADIO_TRANSCRIPTS_PREFIX,
        ):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                modified = item.get("LastModified")
                if not key.endswith(".transcript.json") or not isinstance(modified, datetime):
                    continue
                if modified < cutoff or modified > settled_before:
                    continue
                path = PurePosixPath(key)
                if len(path.parts) < 3:
                    continue
                group_key = path.parent.as_posix() + "/"
                grouped[group_key].append(item)
        output: list[dict[str, Any]] = []
        for group_key, items in grouped.items():
            items.sort(key=lambda item: str(item.get("Key") or ""))
            keys = [str(item["Key"]) for item in items]
            station_id = PurePosixPath(keys[0]).parts[1]
            fingerprint_raw = "|".join(
                f"{item['Key']}:{str(item.get('ETag') or '').strip(chr(34))}"
                for item in items
            )
            output.append(
                {
                    "group_key": group_key,
                    "station_id": station_id,
                    "keys": keys,
                    "fingerprint": hashlib.sha256(
                        fingerprint_raw.encode("utf-8")
                    ).hexdigest(),
                    "last_modified": max(item["LastModified"] for item in items),
                }
            )
        output.sort(key=lambda item: item["last_modified"])
        return output

    @staticmethod
    def _exact_match(full_transcript: str, binding: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [str(binding.get("display_name") or ""), *[
            str(value) for value in binding.get("aliases", [])
        ]]
        token_mode = str(binding.get("match_mode") or "tokens") == "tokens"
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            if token_mode:
                pattern = rf"(?<!\w){re.escape(candidate)}(?!\w)"
                direct = re.search(pattern, full_transcript, flags=re.IGNORECASE)
            else:
                direct = re.search(re.escape(candidate), full_transcript, flags=re.IGNORECASE)
            span = direct.span() if direct else find_normalized_span(full_transcript, candidate)
            if span is None:
                continue
            start, end = span
            if token_mode:
                if start > 0 and full_transcript[start - 1].isalnum():
                    continue
                if end < len(full_transcript) and full_transcript[end].isalnum():
                    continue
            return {
                "is_match": True,
                "match_type": "exact_alias",
                "matched_text": full_transcript[start:end],
                "target_relevance": "direct",
                "summary": None,
                "why_relevant": "An exact normalized keyword or alias appears in the transcript.",
                "sentiment": "neutral",
                "confidence": 1.0,
                "needs_review": False,
                "error": None,
            }
        return None

    def _record_for_match(
        self,
        *,
        binding: dict[str, Any],
        group: dict[str, Any],
        station: dict[str, Any] | None,
        conversation: dict[str, Any],
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        full_transcript = str(conversation["full_transcript"])
        matched_text = str(result["matched_text"])
        match = re.search(re.escape(matched_text), full_transcript, flags=re.IGNORECASE)
        if match is None:
            raise ValueError("Semantic match did not map to a verbatim transcript span")
        start_char, end_char = match.span()
        segment = self._segment_for_char(conversation, start_char)
        words = [
            word
            for word in conversation.get("words", [])
            if int(word.get("end_char", 0)) > start_char
            and int(word.get("start_char", 0)) < end_char
        ]
        mention_start = self._earliest(words, "broadcast_start_utc") or _datetime(
            segment.get("broadcast_start_utc")
        )
        mention_end = self._latest(words, "broadcast_end_utc") or _datetime(
            segment.get("broadcast_end_utc")
        ) or mention_start
        if mention_start is None:
            raise ValueError("Semantic match has no broadcast timestamp")
        source_key = str(segment["source_transcript_key"])
        document = self._load_json(source_key)
        audio_key = self._document_audio_key(document)
        if not audio_key or not is_allowed_audio_key(audio_key):
            raise ValueError("Semantic transcript does not reference an allowed clean audio object")
        clip_start = _datetime(document.get("broadcast_start_utc")) or self._document_start(document)
        clip_end = self._document_end(document) or mention_end or clip_start
        if clip_start is None or clip_end is None:
            raise ValueError("Semantic source clip has incomplete timestamps")
        station = station or {
            "name": group["station_id"],
            "country_code": None,
            "language_codes": [],
        }
        label = str(result.get("sentiment") or "neutral")
        if label == "mixed" or label not in {"positive", "neutral", "negative"}:
            label = "neutral"
        context = str(segment.get("text") or matched_text)
        source_mention_id = hashlib.sha256(
            "|".join(
                [
                    str(binding["campaign_id"]),
                    str(binding["keyword_id"]),
                    str(group["group_key"]),
                    matched_text.casefold(),
                ]
            ).encode("utf-8")
        ).hexdigest()[:32]
        record = {
            "campaign_id": str(binding["campaign_id"]),
            "campaign_keyword_id": str(binding["keyword_id"]),
            "station_id": str(group["station_id"]),
            "station_name": str(station.get("name") or group["station_id"]),
            "station_country_code": station.get("country_code"),
            "station_language_codes": station.get("language_codes") or [],
            "source_result_s3_key": "pending",
            "source_mention_id": source_mention_id,
            "entity_id": str(binding["entity_id"]),
            "display_name": str(binding["display_name"]),
            "matched_alias": matched_text,
            "context": context,
            "detected_language": self._document_language(document),
            "language_probability": self._document_language_probability(document),
            "sentiment_label": label,
            "sentiment_score": _float(result.get("confidence")),
            "sentiment_margin": None,
            "needs_review": bool(result.get("needs_review")),
            "broadcast_start_utc": _iso(mention_start),
            "broadcast_end_utc": _iso(mention_end),
            "audio_clip_start_utc": _iso(clip_start),
            "audio_clip_end_utc": _iso(clip_end),
            "audio_s3_key": audio_key,
            "raw_audio_s3_key": None,
            "transcript_s3_key": source_key,
        }
        audit = {
            "schema_version": "1.0",
            "pipeline_version": "semantic-discovery-0.3.1",
            "campaign_id": binding["campaign_id"],
            "campaign_keyword_id": binding["keyword_id"],
            "station_id": group["station_id"],
            "transcript_group_key": group["group_key"],
            "source_transcript_keys": conversation.get("transcript_source_keys", []),
            "full_transcript": full_transcript,
            "highlight": {
                "start_char": start_char,
                "end_char": end_char,
                "text": full_transcript[start_char:end_char],
                "broadcast_start_utc": _iso(mention_start),
                "broadcast_end_utc": _iso(mention_end),
            },
            "keyword": {
                "value": binding["display_name"],
                "aliases": binding.get("aliases", []),
                "keyword_type": binding.get("keyword_type"),
                "semantic_threshold": binding.get("semantic_threshold"),
            },
            "llm_match": result,
            "processed_at_utc": _iso(datetime.now(timezone.utc)),
        }
        return record, audit

    def _audit_key(self, group: dict[str, Any], binding: dict[str, Any]) -> str:
        date = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        marker = hashlib.sha256(
            f"{binding['keyword_id']}|{group['group_key']}".encode("utf-8")
        ).hexdigest()[:20]
        return (
            f"{self._settings.RADIO_SEMANTIC_RESULTS_PREFIX}"
            f"{group['station_id']}/{date}/{marker}.semantic.json"
        )

    def _record_error(
        self,
        binding: dict[str, Any],
        group: dict[str, Any],
        revision: int,
        error: Exception,
    ) -> None:
        self._database.record_semantic_scan(
            campaign_keyword_id=str(binding["keyword_id"]),
            transcript_group_key=str(group["group_key"]),
            source_fingerprint=str(group["fingerprint"]),
            campaign_revision=revision,
            status="error",
            error=str(error)[:1000],
        )

    def _load_json(self, key: str) -> dict[str, Any]:
        body = self._s3.get_object(
            Bucket=self._settings.RADIO_S3_BUCKET,
            Key=key,
        )["Body"].read()
        loaded = json.loads(body)
        if not isinstance(loaded, dict):
            raise ValueError(f"Transcript is not an object: {key}")
        return loaded

    @staticmethod
    def _segment_for_char(conversation: dict[str, Any], char_index: int) -> dict[str, Any]:
        segments = conversation.get("transcript_segments") or []
        for segment in segments:
            if int(segment.get("start_char", 0)) <= char_index < int(segment.get("end_char", 0)):
                return segment
        if segments:
            return segments[0]
        raise ValueError("Conversation has no transcript segments")

    @staticmethod
    def _earliest(words: list[dict[str, Any]], key: str) -> datetime | None:
        values = [_datetime(word.get(key)) for word in words]
        values = [value for value in values if value]
        return min(values) if values else None

    @staticmethod
    def _latest(words: list[dict[str, Any]], key: str) -> datetime | None:
        values = [_datetime(word.get(key)) for word in words]
        values = [value for value in values if value]
        return max(values) if values else None

    @staticmethod
    def _conversation_start(conversation: dict[str, Any]) -> datetime | None:
        values = [
            _datetime(segment.get("broadcast_start_utc"))
            for segment in conversation.get("transcript_segments", [])
        ]
        values = [value for value in values if value]
        return min(values) if values else None

    @staticmethod
    def _dominant_language(conversation: dict[str, Any]) -> str | None:
        counts: dict[str, int] = defaultdict(int)
        for segment in conversation.get("transcript_segments", []):
            language = str(segment.get("detected_language") or "").strip()
            if language:
                counts[language] += max(1, len(str(segment.get("text") or "")))
        return max(counts, key=counts.get) if counts else None

    def _document_audio_key(self, document: dict[str, Any]) -> str | None:
        """Return the canonical clean-speech key from supported transcript schemas.

        Step 3A production transcript JSON stores the audio reference under
        ``source.audio_s3_uri``. Earlier pilot fixtures used top-level
        ``source_audio`` or ``audio_s3_uri``. Supporting both keeps semantic
        discovery compatible with historical and current transcript objects
        without ever accepting raw-audio keys.
        """
        source = document.get("source")
        source = source if isinstance(source, dict) else {}
        input_document = document.get("input")
        input_document = input_document if isinstance(input_document, dict) else {}
        candidates = (
            document.get("source_audio"),
            document.get("audio_s3_uri"),
            source.get("audio_s3_uri"),
            source.get("source_audio"),
            source.get("clean_audio_s3_uri"),
            input_document.get("audio_s3_uri"),
            input_document.get("source_audio"),
        )
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            key = parse_s3_uri(candidate, self._settings.RADIO_S3_BUCKET)
            if key:
                return key
        return None

    @staticmethod
    def _document_start(document: dict[str, Any]) -> datetime | None:
        direct = _datetime(document.get("broadcast_start_utc"))
        if direct:
            return direct
        values = [
            _datetime(segment.get("broadcast_start_utc"))
            for segment in document.get("segments", [])
            if isinstance(segment, dict)
        ]
        values = [value for value in values if value]
        return min(values) if values else None

    @staticmethod
    def _document_end(document: dict[str, Any]) -> datetime | None:
        values = [
            _datetime(segment.get("broadcast_end_utc"))
            for segment in document.get("segments", [])
            if isinstance(segment, dict)
        ]
        values = [value for value in values if value]
        return max(values) if values else None

    @staticmethod
    def _document_language(document: dict[str, Any]) -> str | None:
        language = document.get("language")
        return str(language.get("detected") or "").strip() or None if isinstance(language, dict) else None

    @staticmethod
    def _document_language_probability(document: dict[str, Any]) -> float | None:
        language = document.get("language")
        return _float(language.get("probability")) if isinstance(language, dict) else None
