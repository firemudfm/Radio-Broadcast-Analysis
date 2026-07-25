from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from .conversation import ConversationService
from .llm import LocalLlmClient

logger = logging.getLogger(__name__)


def _json_body(response: dict[str, Any]) -> dict[str, Any]:
    loaded = json.loads(response["Body"].read())
    if not isinstance(loaded, dict):
        raise ValueError("Analysis object is not a JSON object")
    return loaded


class MentionAnalysisService:
    def __init__(
        self,
        settings: Settings,
        database: Any,
        s3_client: Any,
        conversation_service: ConversationService,
        llm_client: LocalLlmClient,
    ) -> None:
        self._settings = settings
        self._database = database
        self._s3 = s3_client
        self._conversation = conversation_service
        self._llm = llm_client

    def detail(self, mention_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
        mention = self._database.get_mention_detail_record(mention_id)
        if mention is None:
            return None
        conversation = self._conversation.build(mention)
        analysis = (
            self._analysis(mention, conversation, refresh=True)
            if refresh
            else self._cached_or_pending(mention, conversation)
        )
        mention_view = self._database.mention_view_by_id(mention_id)
        if mention_view is None:
            return None
        return {
            "mention": mention_view,
            **conversation,
            "analysis": analysis,
        }

    def analyze(self, mention_id: str, *, force: bool = False) -> dict[str, Any] | None:
        mention = self._database.get_mention_detail_record(mention_id)
        if mention is None:
            return None
        conversation = self._conversation.build(mention)
        analysis = self._analysis(mention, conversation, refresh=force)
        mention_view = self._database.mention_view_by_id(mention_id)
        if mention_view is None:
            return None
        return {
            "mention": mention_view,
            **conversation,
            "analysis": analysis,
        }

    def _cached_or_pending(
        self,
        mention: dict[str, Any],
        conversation: dict[str, Any],
    ) -> dict[str, Any]:
        mention_id = str(mention["id"])
        cache_key = self._cache_key(mention)
        transcript_hash = self._transcript_hash(conversation)
        cached = self._load_cache(cache_key, expected_transcript_hash=transcript_hash)
        if cached:
            self._database.set_analysis_status(
                mention_id,
                status="ready",
                analysis_s3_key=cache_key,
                model=str(cached.get("model") or ""),
                summary=str(cached.get("summary") or "") or None,
                error=None,
            )
            self._database.update_mention_from_analysis(
                mention_id,
                sentiment=cached.get("sentiment"),
                confidence=cached.get("confidence"),
                needs_review=bool(cached.get("needs_review")),
            )
            return cached
        if not self._settings.RADIO_LLM_ENABLED:
            self._database.set_analysis_status(mention_id, status="disabled")
            return self._status_document(
                status="disabled",
                error="Local LLM analysis is disabled",
            )
        state = self._database.analysis_record(mention_id)
        if state and str(state.get("status")) == "error":
            return self._status_document(
                status="error",
                error=str(state.get("error") or "Conversation analysis failed"),
            )
        self._database.set_analysis_status(
            mention_id,
            status="pending",
            model=self._settings.RADIO_LLM_MODEL,
            error=None,
        )
        return self._status_document(status="pending")

    def _status_document(self, *, status: str, error: str | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "model": self._settings.RADIO_LLM_MODEL if status != "disabled" else None,
            "summary": None,
            "why_relevant": None,
            "speaker_intent": None,
            "sentiment": None,
            "target_relevance": None,
            "key_points": [],
            "evidence": [],
            "confidence": None,
            "needs_review": status in {"disabled", "error"},
            "generated_at_utc": None,
            "error": error,
        }

    def _analysis(
        self,
        mention: dict[str, Any],
        conversation: dict[str, Any],
        *,
        refresh: bool,
    ) -> dict[str, Any]:
        mention_id = str(mention["id"])
        cache_key = self._cache_key(mention)
        transcript_hash = self._transcript_hash(conversation)
        if not refresh:
            cached = self._load_cache(cache_key, expected_transcript_hash=transcript_hash)
            if cached:
                self._database.set_analysis_status(
                    mention_id,
                    status="ready",
                    analysis_s3_key=cache_key,
                    model=str(cached.get("model") or ""),
                    summary=str(cached.get("summary") or "") or None,
                )
                self._database.update_mention_from_analysis(
                    mention_id,
                    sentiment=cached.get("sentiment"),
                    confidence=cached.get("confidence"),
                    needs_review=bool(cached.get("needs_review")),
                )
                return cached
        if not self._settings.RADIO_LLM_ENABLED:
            disabled = {
                "status": "disabled",
                "model": None,
                "summary": None,
                "why_relevant": None,
                "speaker_intent": None,
                "sentiment": None,
                "target_relevance": None,
                "key_points": [],
                "evidence": [],
                "confidence": None,
                "needs_review": True,
                "generated_at_utc": None,
                "error": "Local LLM analysis is disabled",
            }
            self._database.set_analysis_status(mention_id, status="disabled")
            return disabled
        self._database.set_analysis_status(
            mention_id,
            status="pending",
            model=self._settings.RADIO_LLM_MODEL,
            increment_attempts=True,
        )
        try:
            result = self._llm.analyze(
                target=str(mention.get("keyword_value") or mention.get("display_name") or ""),
                matched_alias=str(mention.get("matched_alias") or "") or None,
                detected_language=str(mention.get("detected_language") or "") or None,
                highlighted_sentence=conversation.get("highlighted_sentence"),
                full_transcript=str(conversation.get("full_transcript") or ""),
            )
            document = {
                **result,
                "schema_version": "1.0",
                "mention_id": mention_id,
                "campaign_id": str(mention["campaign_id"]),
                "station_id": str(mention["station_id"]),
                "keyword": str(mention.get("keyword_value") or ""),
                "matched_alias": mention.get("matched_alias"),
                "source_result_s3_key": str(mention.get("source_result_s3_key") or ""),
                "source_transcript_keys": conversation.get("transcript_source_keys", []),
                "transcript_sha256": transcript_hash,
            }
            self._s3.put_object(
                Bucket=self._settings.RADIO_S3_BUCKET,
                Key=cache_key,
                Body=json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json; charset=utf-8",
                ServerSideEncryption="AES256",
            )
            self._database.set_analysis_status(
                mention_id,
                status="ready",
                analysis_s3_key=cache_key,
                model=str(result.get("model") or ""),
                summary=str(result.get("summary") or "") or None,
                error=None,
            )
            self._database.update_mention_from_analysis(
                mention_id,
                sentiment=result.get("sentiment"),
                confidence=result.get("confidence"),
                needs_review=bool(result.get("needs_review")),
            )
            return result
        except Exception as error:
            logger.exception("Conversation analysis failed for mention %s", mention_id)
            message = str(error)[:1000]
            self._database.set_analysis_status(
                mention_id,
                status="error",
                model=self._settings.RADIO_LLM_MODEL,
                error=message,
            )
            return {
                "status": "error",
                "model": self._settings.RADIO_LLM_MODEL,
                "summary": None,
                "why_relevant": None,
                "speaker_intent": None,
                "sentiment": None,
                "target_relevance": None,
                "key_points": [],
                "evidence": [],
                "confidence": None,
                "needs_review": True,
                "generated_at_utc": datetime.now(UTC),
                "error": message,
            }

    def _cache_key(self, mention: dict[str, Any]) -> str:
        station = str(mention.get("station_id") or "unknown")
        start = str(mention.get("broadcast_start_utc") or "")
        date = start[:10].replace("-", "/") if len(start) >= 10 else "unknown/date"
        return (
            f"{self._settings.RADIO_ANALYSIS_PREFIX}{station}/{date}/"
            f"{mention['id']}.analysis.json"
        )

    @staticmethod
    def _transcript_hash(conversation: dict[str, Any]) -> str:
        return hashlib.sha256(
            str(conversation.get("full_transcript") or "").encode("utf-8")
        ).hexdigest()

    def _load_cache(
        self,
        key: str,
        *,
        expected_transcript_hash: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            response = self._s3.get_object(Bucket=self._settings.RADIO_S3_BUCKET, Key=key)
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            return None
        try:
            loaded = _json_body(response)
        except Exception:
            return None
        if expected_transcript_hash is not None:
            cached_hash = str(loaded.get("transcript_sha256") or "")
            if cached_hash != expected_transcript_hash:
                return None
        return {
            "status": str(loaded.get("status") or "ready"),
            "model": loaded.get("model"),
            "summary": loaded.get("summary"),
            "why_relevant": loaded.get("why_relevant"),
            "speaker_intent": loaded.get("speaker_intent"),
            "sentiment": loaded.get("sentiment"),
            "target_relevance": loaded.get("target_relevance"),
            "key_points": loaded.get("key_points") if isinstance(loaded.get("key_points"), list) else [],
            "evidence": loaded.get("evidence") if isinstance(loaded.get("evidence"), list) else [],
            "confidence": loaded.get("confidence"),
            "needs_review": bool(loaded.get("needs_review")),
            "generated_at_utc": loaded.get("generated_at_utc"),
            "error": loaded.get("error"),
        }
