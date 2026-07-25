from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..s3_utils import is_allowed_audio_key, parse_s3_uri
from ..text import normalize_text

logger = logging.getLogger(__name__)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _stable_source_mention_id(result_key: str, mention: dict[str, Any], index: int) -> str:
    explicit = str(mention.get("mention_id") or "").strip()
    if explicit:
        return explicit
    raw = "|".join(
        [
            result_key,
            str(index),
            str(mention.get("entity_id") or mention.get("display_name") or ""),
            str(mention.get("broadcast_start_utc") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class IntelligenceSyncService:
    def __init__(
        self,
        settings: Settings,
        database: Any,
        station_service: Any,
        s3_client: Any,
    ) -> None:
        self._settings = settings
        self._database = database
        self._stations = station_service
        self._s3 = s3_client
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_result = self._result(0, 0, 0, 0)

    @property
    def last_result(self) -> dict[str, Any]:
        return dict(self._last_result)

    async def sync_once(self) -> dict[str, Any]:
        async with self._lock:
            bindings = await asyncio.to_thread(self._database.active_bindings)
            objects = await asyncio.to_thread(self._list_result_objects)
            if not bindings:
                result = self._result(len(objects), 0, 0, 0)
                self._last_result = result
                return result
            revision = await asyncio.to_thread(self._database.campaign_revision)
            entity_index, alias_index = self._binding_indexes(bindings)
            station_map = await asyncio.to_thread(self._stations.station_map)
            loaded_count = 0
            mentions_seen = 0
            materialized = 0
            for item in objects:
                key = str(item["Key"])
                etag = str(item.get("ETag") or "").strip('"')
                current = await asyncio.to_thread(
                    self._database.result_is_current, key, etag, revision
                )
                if current:
                    continue
                document = await asyncio.to_thread(self._load_json, key)
                loaded_count += 1
                source = _dict(document.get("source"))
                station_id = str(source.get("station_id") or "").strip()
                records: list[dict[str, Any]] = []
                if not station_id:
                    logger.warning("Skipping result without station id: %s", key)
                else:
                    for index, mention in enumerate(_list(document.get("mentions"))):
                        if not isinstance(mention, dict):
                            continue
                        mentions_seen += 1
                        matches = self._match_bindings(
                            mention,
                            station_id=station_id,
                            entity_index=entity_index,
                            alias_index=alias_index,
                        )
                        for binding in matches:
                            record = self._materialized_record(
                                result_key=key,
                                document=document,
                                mention=mention,
                                mention_index=index,
                                binding=binding,
                                station_id=station_id,
                                station=station_map.get(station_id),
                            )
                            if record is not None:
                                records.append(record)
                materialized += await asyncio.to_thread(
                    self._database.replace_result_mentions,
                    result_key=key,
                    records=records,
                    etag=etag,
                    revision=revision,
                )
            result = self._result(len(objects), loaded_count, mentions_seen, materialized)
            self._last_result = result
            logger.info(
                "Intelligence sync complete objects=%s loaded=%s mentions=%s materialized=%s",
                len(objects),
                loaded_count,
                mentions_seen,
                materialized,
            )
            return result

    def start(self) -> None:
        if not self._settings.RADIO_SYNC_ENABLED or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="radio-intelligence-sync")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sync_once()
            except Exception:
                logger.exception("Intelligence synchronization failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._settings.RADIO_SYNC_INTERVAL_SECONDS
                )
            except TimeoutError:
                continue

    def _list_result_objects(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._settings.RADIO_SYNC_LOOKBACK_DAYS)
        paginator = self._s3.get_paginator("list_objects_v2")
        objects: list[dict[str, Any]] = []
        for page in paginator.paginate(
            Bucket=self._settings.RADIO_S3_BUCKET,
            Prefix=self._settings.RADIO_RESULTS_PREFIX,
        ):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                modified = item.get("LastModified")
                if not key.endswith(".json") or not isinstance(modified, datetime):
                    continue
                if modified < cutoff:
                    continue
                objects.append(item)
        objects.sort(key=lambda item: item["LastModified"], reverse=True)
        return objects[: self._settings.RADIO_SYNC_MAX_OBJECTS]

    def _load_json(self, key: str) -> dict[str, Any]:
        response = self._s3.get_object(Bucket=self._settings.RADIO_S3_BUCKET, Key=key)
        loaded = json.loads(response["Body"].read())
        if not isinstance(loaded, dict):
            raise ValueError(f"Intelligence result is not a JSON object: {key}")
        return loaded

    @staticmethod
    def _binding_indexes(
        bindings: list[dict[str, Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        entity_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        alias_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for binding in bindings:
            entity_index[str(binding["entity_id"])].append(binding)
            for alias in [binding["display_name"], *binding.get("aliases", [])]:
                marker = normalize_text(str(alias))
                if marker:
                    alias_index[marker].append(binding)
        return entity_index, alias_index

    @staticmethod
    def _match_bindings(
        mention: dict[str, Any],
        *,
        station_id: str,
        entity_index: dict[str, list[dict[str, Any]]],
        alias_index: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        entity_id = str(mention.get("entity_id") or "").strip()
        if entity_id:
            candidates.extend(entity_index.get(entity_id, []))
        if not candidates:
            sentiment = _dict(mention.get("sentiment"))
            for value in (
                mention.get("display_name"),
                mention.get("matched_alias"),
                sentiment.get("target"),
            ):
                marker = normalize_text(str(value or ""))
                candidates.extend(alias_index.get(marker, []))
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for binding in candidates:
            marker = str(binding["keyword_id"])
            if marker in seen or station_id not in binding["station_ids"]:
                continue
            seen.add(marker)
            output.append(binding)
        return output

    def _materialized_record(
        self,
        *,
        result_key: str,
        document: dict[str, Any],
        mention: dict[str, Any],
        mention_index: int,
        binding: dict[str, Any],
        station_id: str,
        station: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        source = _dict(document.get("source"))
        start = _datetime(mention.get("broadcast_start_utc")) or _datetime(
            source.get("broadcast_start_utc")
        )
        monitor_from = _datetime(binding["monitor_from_utc"])
        if start is None or monitor_from is None or start < monitor_from:
            return None
        end = _datetime(mention.get("broadcast_end_utc")) or _datetime(
            source.get("broadcast_end_utc")
        )
        clip_start = _datetime(source.get("broadcast_start_utc")) or start
        clip_end = _datetime(source.get("broadcast_end_utc")) or end or start
        if clip_end < clip_start:
            return None
        audio_key = parse_s3_uri(
            str(mention.get("audio_s3_uri") or source.get("audio_s3_uri") or ""),
            self._settings.RADIO_S3_BUCKET,
        )
        if not audio_key or not is_allowed_audio_key(audio_key):
            return None
        sentiment = _dict(mention.get("sentiment"))
        label = str(sentiment.get("label") or "neutral").lower()
        if label not in {"positive", "neutral", "negative"}:
            label = "neutral"
        source_mention_id = _stable_source_mention_id(result_key, mention, mention_index)
        station = station or {
            "name": str(source.get("station_name") or station_id),
            "country_code": None,
            "language_codes": [],
        }
        return {
            "campaign_id": binding["campaign_id"],
            "campaign_keyword_id": binding["keyword_id"],
            "station_id": station_id,
            "station_name": station.get("name") or source.get("station_name") or station_id,
            "station_country_code": station.get("country_code"),
            "station_language_codes": station.get("language_codes", []),
            "source_result_s3_key": result_key,
            "source_mention_id": source_mention_id,
            "entity_id": str(mention.get("entity_id") or binding["entity_id"]),
            "display_name": str(mention.get("display_name") or binding["display_name"]),
            "matched_alias": mention.get("matched_alias"),
            "context": str(mention.get("context") or document.get("transcript_text") or ""),
            "detected_language": mention.get("detected_language") or source.get("detected_language"),
            "language_probability": _float(source.get("language_probability")),
            "sentiment_label": label,
            "sentiment_score": _float(sentiment.get("score")),
            "sentiment_margin": _float(sentiment.get("margin")),
            "needs_review": bool(
                sentiment.get("needs_review") or sentiment.get("low_confidence")
            ),
            "broadcast_start_utc": start.isoformat().replace("+00:00", "Z"),
            "broadcast_end_utc": end.isoformat().replace("+00:00", "Z") if end else None,
            "audio_clip_start_utc": clip_start.isoformat().replace("+00:00", "Z"),
            "audio_clip_end_utc": clip_end.isoformat().replace("+00:00", "Z"),
            "audio_s3_key": audio_key,
            "raw_audio_s3_key": parse_s3_uri(
                str(source.get("raw_audio_s3_uri") or ""), self._settings.RADIO_S3_BUCKET
            ),
            "transcript_s3_key": parse_s3_uri(
                str(_dict(document.get("input")).get("transcript_s3_uri") or ""),
                self._settings.RADIO_S3_BUCKET,
            ),
        }

    @staticmethod
    def _result(
        objects_scanned: int,
        objects_loaded: int,
        mentions_seen: int,
        mentions_materialized: int,
    ) -> dict[str, Any]:
        return {
            "objects_scanned": objects_scanned,
            "objects_loaded": objects_loaded,
            "mentions_seen": mentions_seen,
            "mentions_materialized": mentions_materialized,
            "completed_at_utc": datetime.now(timezone.utc),
        }
