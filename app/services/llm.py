from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..config import Settings

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_ALLOWED_SENTIMENT = {"positive", "neutral", "negative", "mixed"}
_ALLOWED_RELEVANCE = {"direct", "indirect", "incidental", "not_relevant"}
_ALLOWED_MATCH_TYPES = {"exact_alias", "phonetic_entity", "translated_equivalent", "semantic_topic", "not_relevant"}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_list(value: Any, *, maximum: int, max_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        text = text[:max_length]
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        output.append(text)
        if len(output) >= maximum:
            break
    return output


class LocalLlmClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.RADIO_LLM_BASE_URL.rstrip("/")

    def health(self) -> bool:
        if not self._settings.RADIO_LLM_ENABLED:
            return False
        request = urllib.request.Request(f"{self._base_url}/health", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    def analyze(
        self,
        *,
        target: str,
        matched_alias: str | None,
        detected_language: str | None,
        highlighted_sentence: str | None,
        full_transcript: str,
    ) -> dict[str, Any]:
        if not self._settings.RADIO_LLM_ENABLED:
            return {
                "status": "disabled",
                "model": None,
                "key_points": [],
                "evidence": [],
                "needs_review": True,
                "error": "Local LLM analysis is disabled",
            }
        transcript_for_model = full_transcript[: self._settings.RADIO_LLM_MAX_INPUT_CHARACTERS]
        system = (
            "You are a careful multilingual radio-monitoring analyst. "
            "Use only the supplied transcript. Analyze the named target, not the general mood of unrelated entities. "
            "Never invent facts. Evidence must be short verbatim phrases found in the transcript. "
            "Return one JSON object and no markdown. Do not expose hidden reasoning."
        )
        user = f"""/no_think
Target keyword or entity: {target}
Matched on-air alias: {matched_alias or 'not provided'}
Detected transcript language: {detected_language or 'unknown'}
Highlighted sentence: {highlighted_sentence or 'not available'}

Complete available transcript for this radio chunk:
---
{transcript_for_model}
---

Return exactly these JSON fields:
summary: concise description of the complete discussion
why_relevant: why the transcript is or is not relevant to the target
speaker_intent: informative, promotional, critical, endorsement, complaint, comparison, incidental, or unclear
sentiment: positive, neutral, negative, or mixed toward the target
 target_relevance: direct, indirect, incidental, or not_relevant
key_points: array of up to 5 concise points
 evidence: array of up to 3 short verbatim transcript phrases
confidence: number from 0 to 1
needs_review: boolean
"""
        payload = {
            "model": self._settings.RADIO_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._settings.RADIO_LLM_TEMPERATURE,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.0,
            "max_tokens": self._settings.RADIO_LLM_MAX_OUTPUT_TOKENS,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        raw = self._request(payload)
        parsed = self._parse_json(raw)
        sentiment = str(parsed.get("sentiment") or "neutral").strip().lower()
        if sentiment not in _ALLOWED_SENTIMENT:
            sentiment = "neutral"
        relevance = str(parsed.get("target_relevance") or "incidental").strip().lower()
        if relevance not in _ALLOWED_RELEVANCE:
            relevance = "incidental"
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        evidence = self._validated_evidence(
            _clean_list(parsed.get("evidence"), maximum=3, max_length=240),
            full_transcript,
        )
        needs_review = bool(parsed.get("needs_review")) or confidence < 0.60
        if relevance in {"incidental", "not_relevant"} and sentiment != "neutral":
            # Prevent sentence-level sentiment from leaking to an entity that is merely named.
            sentiment = "neutral"
            needs_review = needs_review or relevance == "incidental"
        return {
            "status": "ready",
            "model": self._settings.RADIO_LLM_MODEL,
            "summary": str(parsed.get("summary") or "").strip()[:1200] or None,
            "why_relevant": str(parsed.get("why_relevant") or "").strip()[:1200] or None,
            "speaker_intent": str(parsed.get("speaker_intent") or "unclear").strip()[:80],
            "sentiment": sentiment,
            "target_relevance": relevance,
            "key_points": _clean_list(parsed.get("key_points"), maximum=5, max_length=300),
            "evidence": evidence,
            "confidence": confidence,
            "needs_review": needs_review,
            "generated_at_utc": utc_iso(),
            "error": None,
        }

    def match_keyword(
        self,
        *,
        target: str,
        aliases: list[str],
        keyword_type: str,
        detected_language: str | None,
        full_transcript: str,
        threshold: float,
    ) -> dict[str, Any]:
        """Find a cross-language target mention in the whole available transcript.

        This method is used only for keywords that explicitly enable semantic
        matching. It requires a verbatim on-air span, so every accepted match can
        be highlighted and mapped back to audio timestamps.
        """
        if not self._settings.RADIO_LLM_ENABLED:
            return {
                "is_match": False,
                "match_type": "not_relevant",
                "matched_text": None,
                "confidence": 0.0,
                "needs_review": True,
                "error": "Local LLM analysis is disabled",
            }
        transcript_for_model = full_transcript[: self._settings.RADIO_LLM_MAX_INPUT_CHARACTERS]
        alias_text = ", ".join(aliases[:25]) if aliases else "none"
        strict_entity = keyword_type in {"brand", "person", "product", "organization"}
        entity_rule = (
            "This is a named entity. Do not accept a translation, related category, competitor, or generic concept. "
            "Accept only the same named entity, a clear spoken/phonetic spelling variant, or an explicit alias."
            if strict_entity
            else
            "This is a concept/topic. A clear equivalent meaning in another language may match, but merely related words do not."
        )
        system = (
            "You are a conservative multilingual radio mention verifier. "
            "Use only the supplied transcript. Never invent a phrase. "
            "An accepted matched_text must be copied verbatim from the transcript. "
            "Return one JSON object and no markdown. Do not reveal hidden reasoning."
        )
        user = f"""/no_think
Target: {target}
Target type: {keyword_type}
Known aliases: {alias_text}
Detected transcript language: {detected_language or 'unknown'}
Acceptance threshold: {threshold:.2f}
Rule: {entity_rule}

Complete available transcript for this radio chunk:
---
{transcript_for_model}
---

Return exactly these JSON fields:
is_match: boolean
matched_text: exact verbatim phrase copied from the transcript, or null
match_type: exact_alias, phonetic_entity, translated_equivalent, semantic_topic, or not_relevant
target_relevance: direct, indirect, incidental, or not_relevant
summary: concise description of what the transcript says about the target
why_relevant: concise reason for accepting or rejecting
sentiment: positive, neutral, negative, or mixed toward the target
confidence: number from 0 to 1
needs_review: boolean
"""
        payload = {
            "model": self._settings.RADIO_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.0,
            "max_tokens": min(360, self._settings.RADIO_LLM_MAX_OUTPUT_TOKENS),
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        parsed = self._parse_json(self._request(payload))
        is_match = bool(parsed.get("is_match"))
        match_type = str(parsed.get("match_type") or "not_relevant").strip().lower()
        if match_type not in _ALLOWED_MATCH_TYPES:
            match_type = "not_relevant"
        relevance = str(parsed.get("target_relevance") or "not_relevant").strip().lower()
        if relevance not in _ALLOWED_RELEVANCE:
            relevance = "not_relevant"
        sentiment = str(parsed.get("sentiment") or "neutral").strip().lower()
        if sentiment not in _ALLOWED_SENTIMENT:
            sentiment = "neutral"
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        matched_text = str(parsed.get("matched_text") or "").strip() or None
        if matched_text and matched_text.casefold() not in full_transcript.casefold():
            matched_text = None
        # No accepted semantic match is allowed without auditable verbatim evidence.
        accepted = (
            is_match
            and matched_text is not None
            and confidence >= threshold
            and relevance in {"direct", "indirect"}
            and match_type != "not_relevant"
        )
        if strict_entity and match_type in {"translated_equivalent", "semantic_topic"}:
            accepted = False
        if not accepted:
            sentiment = "neutral"
        return {
            "is_match": accepted,
            "match_type": match_type if accepted else "not_relevant",
            "matched_text": matched_text if accepted else None,
            "target_relevance": relevance if accepted else "not_relevant",
            "summary": str(parsed.get("summary") or "").strip()[:1200] or None,
            "why_relevant": str(parsed.get("why_relevant") or "").strip()[:1200] or None,
            "sentiment": sentiment,
            "confidence": confidence,
            "needs_review": bool(parsed.get("needs_review")) or confidence < max(threshold, 0.80),
            "error": None,
        }

    def _request(self, payload: dict[str, Any]) -> str:
        request = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._settings.RADIO_LLM_TIMEOUT_SECONDS,
            ) as response:
                document = json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Local LLM returned HTTP {error.code}: {body}") from error
        except Exception as error:
            raise RuntimeError(f"Local LLM request failed: {error}") from error
        try:
            content = document["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("Local LLM response did not contain chat content") from error
        return str(content)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = _JSON_BLOCK.search(text)
            if not match:
                raise RuntimeError("Local LLM did not return valid JSON")
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as error:
                raise RuntimeError("Local LLM returned malformed JSON") from error
        if not isinstance(parsed, dict):
            raise RuntimeError("Local LLM result was not a JSON object")
        return parsed

    @staticmethod
    def _validated_evidence(values: list[str], transcript: str) -> list[str]:
        folded = transcript.casefold()
        return [value for value in values if value.casefold() in folded]
