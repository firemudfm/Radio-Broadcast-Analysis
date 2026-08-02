"""Structured conversation analysis with the local Qwen model (ADR-007).

Where the LLM sits
------------------
**After** a keyword match, never before, and exactly once per conversation. The
matcher decides that a mention exists; the model only explains one that already
exists. It is never consulted per audio segment, and it can never create a
mention -- an analysis that says "this is about NVIDIA" for a conversation the
matcher found nothing in produces no mention at all.

That ordering is what keeps a 0.6B model affordable on 4 vCPUs: it runs on the
small fraction of audio that already earned it.

Trust boundary
--------------
Model output is untrusted input, so every field is validated:

* evidence text must occur **verbatim in the transcript** -- a quoted phrase
  that was never broadcast is the single worst failure this system could have;
* evidence timestamps must fall inside the conversation;
* enumerations must be members of their declared sets;
* confidence must be within range;
* unknown fields are rejected outright.

Invalid output gets one bounded repair attempt, then a deterministic fallback.
The fallback is a real, useful record -- the transcript and the matched
keywords are already known without the model -- so a wedged LLM degrades the
richness of a mention rather than losing it.

Reasoning traces are never stored or returned. Non-thinking mode is requested
where supported, and any ``<think>`` block that arrives anyway is stripped
before parsing.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import Settings
from ..observability import log_fields
from ..pipeline.enums import (
    ENTITY_TYPES,
    SENTIMENTS,
    SPEAKER_STANCES,
    URGENCIES,
    ContentType,
    EntityType,
    Sentiment,
    SpeakerStance,
    Urgency,
)
from ..pipeline.errors import AnalysisFailedError

logger = logging.getLogger(__name__)

ANALYSIS_SCHEMA_VERSION = "1"

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

#: Caps on repeated fields, so one bad generation cannot produce an unbounded row.
MAX_ENTITIES = 20
MAX_KEY_POINTS = 10
MAX_EVIDENCE = 10


# --- validated result ---------------------------------------------------------


# The response grammar no longer bounds string lengths (see
# RESPONSE_JSON_SCHEMA below), so the bounds are enforced here -- and they
# TRUNCATE rather than reject. A model that rambles past a cap has still found
# the mention; failing validation over length would discard a real analysis
# and replace it with the no-model fallback, which is strictly worse than a
# shortened summary.


def _truncated(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    type: EntityType = "other"

    @field_validator("name", mode="before")
    @classmethod
    def truncate_name(cls, value: Any) -> Any:
        return _truncated(value, 200)

    @field_validator("type", mode="before")
    @classmethod
    def coerce_type(cls, value: Any) -> str:
        candidate = str(value or "other").strip().lower()
        return candidate if candidate in ENTITY_TYPES else "other"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=1000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @field_validator("text", mode="before")
    @classmethod
    def truncate_text(cls, value: Any) -> Any:
        # A truncated quote may no longer match the transcript verbatim, in
        # which case the evidence verifier downstream drops it and flags the
        # result for review -- the right outcome for an over-length quote.
        return _truncated(value, 1000)


class AnalysisResult(BaseModel):
    """The contract the analysis worker persists and the API returns."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = ANALYSIS_SCHEMA_VERSION
    content_type: str = "unknown"
    language: str | None = None
    relevant: bool = True
    summary: str = Field(default="", max_length=2000)
    translated_summary: str | None = Field(default=None, max_length=2000)
    main_topic: str | None = Field(default=None, max_length=300)
    sentiment: Sentiment = "neutral"
    speaker_stance: SpeakerStance = "unclear"
    urgency: Urgency = "normal"
    entities: list[Entity] = Field(default_factory=list, max_length=MAX_ENTITIES)
    key_points: list[str] = Field(default_factory=list, max_length=MAX_KEY_POINTS)
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    status: str = "ready"
    model: str | None = None

    @field_validator("summary", "translated_summary", mode="before")
    @classmethod
    def truncate_summaries(cls, value: Any) -> Any:
        return _truncated(value, 2000)

    @field_validator("main_topic", mode="before")
    @classmethod
    def truncate_main_topic(cls, value: Any) -> Any:
        return _truncated(value, 300)

    @field_validator("sentiment", mode="before")
    @classmethod
    def coerce_sentiment(cls, value: Any) -> str:
        candidate = str(value or "neutral").strip().lower()
        return candidate if candidate in SENTIMENTS else "neutral"

    @field_validator("speaker_stance", mode="before")
    @classmethod
    def coerce_stance(cls, value: Any) -> str:
        candidate = str(value or "unclear").strip().lower()
        return candidate if candidate in SPEAKER_STANCES else "unclear"

    @field_validator("urgency", mode="before")
    @classmethod
    def coerce_urgency(cls, value: Any) -> str:
        candidate = str(value or "normal").strip().lower()
        return candidate if candidate in URGENCIES else "normal"

    @field_validator("key_points", mode="before")
    @classmethod
    def clean_key_points(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:500] for item in value if str(item).strip()][:MAX_KEY_POINTS]

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


#: JSON schema handed to llama.cpp so the model is constrained at decode time
#: rather than merely asked politely for JSON.
#:
#: DELIBERATELY NO maxLength ANYWHERE. llama.cpp compiles a string bound into a
#: bounded grammar repetition -- ``char{0,2000}`` -- and its grammar parser
#: refuses large ones outright:
#:
#:   parse: error parsing grammar: number of rules that are going to be
#:   repeated multiplied by the new repetition exceeds sane defaults
#:
#: which failed EVERY analysis call in production; the model never ran and every
#: mention carried the deterministic fallback instead. Length limits live in
#: the pydantic model below, which truncates after decoding. maxItems stays,
#: because those bounds are tiny (a {0,19} repetition parses fine) and they cap
#: the number of objects generated, which truncation cannot do afterwards.
RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "sentiment", "relevant", "confidence"],
    "properties": {
        "content_type": {"type": "string"},
        "language": {"type": "string"},
        "relevant": {"type": "boolean"},
        "summary": {"type": "string"},
        "translated_summary": {"type": "string"},
        "main_topic": {"type": "string"},
        "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
        "speaker_stance": {"type": "string", "enum": list(SPEAKER_STANCES)},
        "urgency": {"type": "string", "enum": list(URGENCIES)},
        "entities": {
            "type": "array",
            "maxItems": MAX_ENTITIES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                },
            },
        },
        "key_points": {
            "type": "array",
            "maxItems": MAX_KEY_POINTS,
            "items": {"type": "string"},
        },
        "evidence": {
            "type": "array",
            "maxItems": MAX_EVIDENCE,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 0},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


# --- request context ----------------------------------------------------------


@dataclass(frozen=True)
class AnalysisRequest:
    """Everything the model is allowed to see about one conversation."""

    conversation_id: str
    transcript: str
    language: str | None
    content_type: ContentType
    duration_ms: int
    matched_keywords: tuple[str, ...] = ()
    station_name: str = ""


# --- circuit breaker ----------------------------------------------------------


class CircuitBreaker:
    """Stops hammering a dependency that is already failing.

    A 0.6B model on shared CPUs degrades under load rather than erroring
    cleanly, so a queue of analysis jobs that all time out will keep timing out
    and starve everything else. Opening the circuit converts that into fast,
    cheap fallbacks until the model has had time to recover.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._clock() - self._opened_at >= self._reset_seconds:
            # Half-open: allow one probe through rather than reopening blindly.
            self._opened_at = None
            self._failures = self._threshold - 1
            return False
        return True

    @property
    def failure_count(self) -> int:
        return self._failures

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold and self._opened_at is None:
            self._opened_at = self._clock()
            logger.warning(
                "LLM circuit breaker opened", extra=log_fields(failures=self._failures)
            )


# --- client -------------------------------------------------------------------


class LlamaServerClient:
    """Minimal OpenAI-compatible client for llama.cpp's ``llama-server``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.RADIO_LLM_BASE_URL.rstrip("/")

    @property
    def model(self) -> str:
        return self._settings.RADIO_LLM_MODEL

    def health(self) -> bool:
        try:
            request = urllib.request.Request(f"{self._base_url}/health", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310
                return 200 <= response.status < 300
        except Exception:  # noqa: BLE001 - health is advisory, never fatal
            return False

    def complete(self, messages: Sequence[dict[str, str]], *, schema: dict | None = None) -> str:
        settings = self._settings
        payload: dict[str, Any] = {
            "model": settings.RADIO_LLM_MODEL,
            "messages": list(messages),
            "temperature": settings.RADIO_LLM_TEMPERATURE,
            "max_tokens": settings.RADIO_LLM_MAX_OUTPUT_TOKENS,
            "stream": False,
            # Qwen3 exposes reasoning as a toggle. Structured extraction does
            # not benefit from it and it costs tokens we have budgeted for
            # output, so it is switched off where the server understands it.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema is not None and settings.RADIO_LLM_STRICT_SCHEMA:
            # llama.cpp compiles this into a GBNF grammar, so malformed JSON
            # becomes impossible at decode time instead of being caught after.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "radio_analysis", "schema": schema, "strict": True},
            }

        request = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # nosec B310 - internal URL from validated settings
                request, timeout=settings.RADIO_LLM_TIMEOUT_SECONDS
            ) as response:
                document = json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:500]
            raise AnalysisFailedError(
                f"Local LLM returned HTTP {error.code}", detail=body
            ) from error
        except Exception as error:  # noqa: BLE001 - network failures are retryable
            raise AnalysisFailedError(
                "Local LLM request failed", detail=f"{type(error).__name__}: {error}"
            ) from error

        try:
            return str(document["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise AnalysisFailedError("Local LLM response had no chat content") from error


@dataclass
class FakeLlmClient:
    """Deterministic client for tests and for running without a model.

    Returns whatever ``responses`` supplies, so tests can exercise the *real*
    validation, repair and fallback paths against malformed output instead of
    asserting on a mock.
    """

    responses: list[str] = field(default_factory=list)
    model: str = "fake-qwen"
    healthy: bool = True
    calls: list[list[dict[str, str]]] = field(default_factory=list)
    failure: Exception | None = None

    def health(self) -> bool:
        return self.healthy

    def complete(self, messages: Sequence[dict[str, str]], *, schema: dict | None = None) -> str:
        self.calls.append([dict(message) for message in messages])
        if self.failure is not None:
            raise self.failure
        if not self.responses:
            return "{}"
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


# --- service ------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You analyse radio broadcast transcripts. Reply with a single JSON object and "
    "nothing else. Quote evidence VERBATIM from the transcript; never paraphrase "
    "or invent a quote. If the transcript does not support a field, omit it."
)


class ConversationAnalyzer:
    """Runs one analysis per conversation and validates everything it returns."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or LlamaServerClient(settings)
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=settings.RADIO_LLM_CIRCUIT_FAILURE_THRESHOLD,
            reset_seconds=settings.RADIO_LLM_CIRCUIT_RESET_SECONDS,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """Analyse one conversation. Always returns a usable result."""
        if not self._settings.RADIO_LLM_ENABLED:
            return self._fallback(request, status="disabled", reason="LLM disabled by policy")
        if self._breaker.is_open:
            # Fail fast and cheaply; the fallback still carries the transcript
            # and the matched keywords.
            return self._fallback(
                request, status="fallback", reason="LLM circuit breaker is open"
            )

        attempts = 1 + max(0, self._settings.RADIO_LLM_REPAIR_RETRIES)
        last_error: str = "unknown"
        for attempt in range(attempts):
            try:
                content = self._client.complete(
                    self._messages(request, repair=attempt > 0), schema=RESPONSE_JSON_SCHEMA
                )
            except AnalysisFailedError as error:
                self._breaker.record_failure()
                last_error = f"{error.code}: {error.message}"
                continue
            except Exception as error:  # noqa: BLE001 - never let the worker die here
                self._breaker.record_failure()
                last_error = f"{type(error).__name__}: {error}"
                continue

            try:
                result = self._parse_and_validate(content, request)
            except ValueError as error:
                last_error = str(error)[:300]
                logger.info(
                    "LLM output rejected by validation",
                    extra=log_fields(
                        conversation_id=request.conversation_id,
                        attempt=attempt + 1,
                        reason=last_error,
                    ),
                )
                continue

            self._breaker.record_success()
            return result

        self._breaker.record_failure()
        return self._fallback(request, status="fallback", reason=last_error)

    # -- prompt ---------------------------------------------------------------

    def _messages(self, request: AnalysisRequest, *, repair: bool) -> list[dict[str, str]]:
        transcript = request.transcript[: self._settings.RADIO_LLM_MAX_INPUT_CHARACTERS]
        keywords = ", ".join(request.matched_keywords[:20])
        instruction = (
            f"Matched keywords: {keywords or '(none)'}\n"
            f"Detected language: {request.language or 'unknown'}\n"
            f"Content type: {request.content_type}\n\n"
            f"Transcript:\n{transcript}\n\n"
            "Return JSON with: content_type, language, relevant, summary, "
            "translated_summary (English), main_topic, sentiment, speaker_stance, "
            "urgency, entities, key_points, evidence (verbatim quotes with "
            "start_ms/end_ms), confidence."
        )
        if repair:
            instruction = (
                "Your previous reply was not valid for the required schema. "
                "Return ONLY a single valid JSON object matching it, with every "
                "evidence quote copied verbatim from the transcript.\n\n" + instruction
            )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]

    # -- validation -----------------------------------------------------------

    def _parse_and_validate(self, content: str, request: AnalysisRequest) -> AnalysisResult:
        document = _parse_json(content)
        try:
            result = AnalysisResult.model_validate(
                {
                    **document,
                    "schema_version": ANALYSIS_SCHEMA_VERSION,
                    "model": getattr(self._client, "model", None),
                    "status": "ready",
                }
            )
        except ValidationError as error:
            raise ValueError(f"schema validation failed: {error.error_count()} error(s)") from error

        evidence = _validated_evidence(result.evidence, request)
        dropped = len(result.evidence) - len(evidence)
        needs_review = result.needs_review or dropped > 0 or result.confidence < 0.5
        if dropped:
            logger.info(
                "Dropped unverifiable evidence quotes",
                extra=log_fields(conversation_id=request.conversation_id, dropped=dropped),
            )
        return result.model_copy(
            update={
                "evidence": evidence,
                "needs_review": needs_review,
                "language": result.language or request.language,
                "content_type": result.content_type or request.content_type,
            }
        )

    # -- fallback -------------------------------------------------------------

    def _fallback(self, request: AnalysisRequest, *, status: str, reason: str) -> AnalysisResult:
        """A useful record built without the model.

        The mention is already proven: the matcher found the keyword and the
        transcript is on disk. Losing the analysis must not lose the mention.
        """
        summary = request.transcript.strip()
        if len(summary) > 400:
            summary = summary[:397].rsplit(" ", 1)[0] + "..."
        logger.info(
            "Using deterministic analysis fallback",
            extra=log_fields(conversation_id=request.conversation_id, reason=reason[:200]),
        )
        return AnalysisResult(
            schema_version=ANALYSIS_SCHEMA_VERSION,
            content_type=request.content_type,
            language=request.language,
            relevant=True,
            summary=summary,
            main_topic=request.matched_keywords[0] if request.matched_keywords else None,
            sentiment="neutral",
            speaker_stance="unclear",
            urgency="normal",
            entities=[
                Entity(name=keyword, type="other") for keyword in request.matched_keywords[:MAX_ENTITIES]
            ],
            key_points=[],
            evidence=[],
            confidence=0.0,
            needs_review=True,
            status=status,
            model=None,
        )


# --- helpers ------------------------------------------------------------------


def _parse_json(content: str) -> dict[str, Any]:
    text = _THINK_BLOCK.sub("", str(content or "")).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise ValueError("response was not JSON") from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            raise ValueError("response contained malformed JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("response was not a JSON object")
    return parsed


def _validated_evidence(
    evidence: Iterable[Evidence], request: AnalysisRequest
) -> list[Evidence]:
    """Keep only quotes that were really broadcast, with plausible timings.

    A fabricated quote attributed to a radio station is the worst output this
    system could produce, so an unverifiable quote is dropped rather than
    softened or flagged.
    """
    haystack = request.transcript.casefold()
    kept: list[Evidence] = []
    for item in evidence:
        needle = item.text.strip()
        if not needle or needle.casefold() not in haystack:
            continue
        start = max(0, item.start_ms)
        end = max(start, item.end_ms)
        if request.duration_ms:
            # Timestamps outside the conversation would cut the wrong audio.
            if start > request.duration_ms:
                continue
            end = min(end, request.duration_ms)
        kept.append(Evidence(text=needle, start_ms=start, end_ms=end))
    return kept


def build_analyzer(settings: Settings, *, client: Any | None = None) -> ConversationAnalyzer:
    return ConversationAnalyzer(settings, client=client)


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "MAX_ENTITIES",
    "MAX_EVIDENCE",
    "MAX_KEY_POINTS",
    "RESPONSE_JSON_SCHEMA",
    "AnalysisRequest",
    "AnalysisResult",
    "CircuitBreaker",
    "ConversationAnalyzer",
    "Entity",
    "Evidence",
    "FakeLlmClient",
    "LlamaServerClient",
    "build_analyzer",
]
