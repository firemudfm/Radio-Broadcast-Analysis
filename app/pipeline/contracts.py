"""Versioned SQS message contracts (ADR-003).

Rules enforced here, not by convention elsewhere:

* Every message declares ``schema``; consumers accept an explicit allowlist.
* Nothing that could be a secret, a credential, a presigned URL or audio bytes
  may appear in a message.
* Transcripts travel by reference, never inline.
* Serialised size is checked against ``MAX_MESSAGE_BYTES`` — a self-imposed
  64 KiB ceiling, ~16x below the documented 1 MiB SQS limit — so oversized
  payloads fail in our validator with a named error rather than at the AWS API
  boundary.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import AudioContentClass, MatchLevel
from .errors import InvalidMessageError, MessageTooLargeError, UnsupportedSchemaError
from .ids import IdentifierError, validate_station_id, validate_uuid

TRANSCRIPTION_SCHEMA_V1 = "radio.transcription.v1"
ANALYSIS_SCHEMA_V1 = "radio.analysis.v1"

SUPPORTED_TRANSCRIPTION_SCHEMAS: frozenset[str] = frozenset({TRANSCRIPTION_SCHEMA_V1})
SUPPORTED_ANALYSIS_SCHEMAS: frozenset[str] = frozenset({ANALYSIS_SCHEMA_V1})

#: Self-imposed ceiling. SQS allows 1 MiB; we never want to be near it.
MAX_MESSAGE_BYTES = 65_536

#: Caps on repeated fields. Overflow is truncated with an explicit flag rather
#: than silently dropped, so a downstream consumer can tell the difference
#: between "no more matches" and "we stopped counting".
MAX_MATCHED_KEYWORDS = 50
MAX_CAMPAIGN_IDS = 200
MAX_LANGUAGE_HINTS = 8


class MessageModel(BaseModel):
    """Strict base for every wire contract."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    def to_body(self) -> str:
        """Serialise and enforce the size ceiling.

        Size is checked on the exact bytes that will be sent, after
        serialisation, because field-level limits alone cannot bound a JSON
        document.
        """
        body = self.model_dump_json(by_alias=True)
        encoded = len(body.encode("utf-8"))
        if encoded > MAX_MESSAGE_BYTES:
            raise MessageTooLargeError(
                f"Message body is {encoded} bytes, above the {MAX_MESSAGE_BYTES}-byte limit",
                detail=f"schema={getattr(self, 'schema_name', 'unknown')}",
            )
        return body


class StorageDescriptor(MessageModel):
    """Where a segment's bytes live, carried with the message.

    Self-describing on purpose: a consumer must never infer the backend from
    its own configuration, because that is exactly how a message produced in
    one mode gets misread in another.
    """

    backend: Literal["local", "s3"]
    path: str | None = None
    bucket: str | None = None
    key: str | None = None
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=1, le=512 * 1024 * 1024)

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not all(character in "0123456789abcdef" for character in cleaned):
            raise ValueError("sha256 must be lower-case hexadecimal")
        return cleaned

    @model_validator(mode="after")
    def validate_backend_fields(self) -> StorageDescriptor:
        if self.backend == "local":
            if not self.path:
                raise ValueError("local storage requires 'path'")
            if self.bucket or self.key:
                raise ValueError("local storage must not set 'bucket' or 'key'")
        else:
            if not self.bucket or not self.key:
                raise ValueError("s3 storage requires 'bucket' and 'key'")
            if self.path:
                raise ValueError("s3 storage must not set 'path'")
            if "://" in self.key or self.key.startswith("/"):
                raise ValueError("s3 key must be a plain object key, not a URI")
        return self


class TranscriptionJobV1(MessageModel):
    """Listener -> transcription worker.

    ``MessageGroupId`` is ``station_id`` and ``MessageDeduplicationId`` is
    ``segment_id``; see :meth:`message_group_id` / :meth:`deduplication_id`.
    """

    schema_name: Literal["radio.transcription.v1"] = Field(
        default=TRANSCRIPTION_SCHEMA_V1, alias="schema"
    )
    job_id: str
    segment_id: str
    station_id: str
    station_session_id: str
    sequence_number: int = Field(ge=0)
    started_at: datetime
    duration_ms: int = Field(ge=1, le=3_600_000)
    content_class: AudioContentClass
    language_hints: list[str] = Field(default_factory=list, max_length=MAX_LANGUAGE_HINTS)
    keyword_index_version: int = Field(ge=0)
    storage: StorageDescriptor
    trace_id: str
    created_at: datetime

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, frozen=True, populate_by_name=True
    )

    @field_validator("job_id", "segment_id", "station_session_id", "trace_id")
    @classmethod
    def validate_uuid_fields(cls, value: str) -> str:
        return validate_uuid(value)

    @field_validator("station_id")
    @classmethod
    def validate_station(cls, value: str) -> str:
        return validate_station_id(value)

    @field_validator("language_hints")
    @classmethod
    def validate_language_hints(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            code = str(value).strip().lower()
            # BCP-47-ish primary subtag with an optional region; anything else
            # is a configuration error we would rather surface than pass to ASR.
            if not code or len(code) > 8 or not code.replace("-", "").isalnum():
                raise ValueError(f"Invalid language hint: {value!r}")
            if code not in cleaned:
                cleaned.append(code)
        return cleaned

    @field_validator("started_at", "created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    def message_group_id(self) -> str:
        """Per-station ordering. See ADR-003 for the throughput consequence."""
        return self.station_id

    def deduplication_id(self) -> str:
        return self.segment_id


class MatchedKeywordRef(MessageModel):
    """A keyword hit, carried with the evidence needed to reproduce it.

    This is an **evidence-preserving** record, not a pointer. Everything the
    result writer persists into ``mention_keywords`` has to survive the queue
    hop, because the analysis worker has no way to recompute it: it never sees
    the audio, the per-segment transcript, or the station's keyword index.

    Anything omitted here is not "defaulted downstream", it is *fabricated*
    downstream -- an alias hit becomes ``exact``, a candidate becomes
    ``confirmed``, and a real confidence becomes ``1.0``. That lands in the
    permanent audit trail, so the fields below are load-bearing.

    Ownership: ``campaign_ids`` here is the set of campaigns that registered
    **this specific keyword** on this station. It is deliberately narrower than
    :attr:`AnalysisJobV1.campaign_ids`, which covers the whole conversation.

    Coordinates (see :class:`app.services.conversation_assembler.ClosedConversation`):

    * ``start_char``/``end_char`` index into the conversation's assembled
      ``transcript_text``, matching the convention the legacy transcript API
      already uses (``app/services/conversation.py``).
    * ``start_ms``/``end_ms`` are relative to the start of the conversation.

    Every field added in this record is optional with a documented default, so
    a ``radio.analysis.v1`` message serialised before they existed still
    parses. The schema string is unchanged on purpose: additive optional
    fields do not warrant a v2 contract.
    """

    keyword_id: str
    canonical_value: str = Field(min_length=1, max_length=200)
    matched_text: str = Field(min_length=1, max_length=300)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    # --- additive v1 fields ---------------------------------------------------
    #
    # Defaults exist ONLY so a message queued before this change still parses.
    # The producer always populates them; see
    # `app/workers/transcription.py::_commit_conversation`.

    #: Campaigns owning THIS keyword. Empty only in a pre-change message, where
    #: the consumer falls back to the job-level campaign list.
    campaign_ids: list[str] = Field(default_factory=list, max_length=MAX_CAMPAIGN_IDS)
    #: How the hit was made. ``exact`` is the legacy-message fallback, never a
    #: substitute for a real level.
    match_level: MatchLevel = "exact"
    start_char: int = Field(default=0, ge=0)
    #: ``None`` in a legacy message; the consumer derives it from the matched
    #: text length in that case only.
    end_char: int | None = Field(default=None, ge=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("keyword_id")
    @classmethod
    def validate_keyword_id(cls, value: str) -> str:
        return validate_uuid(value, field="keyword_id")

    @field_validator("campaign_ids")
    @classmethod
    def validate_campaign_ids(cls, values: list[str]) -> list[str]:
        """Validate and de-duplicate while preserving order.

        Order-preserving rather than sorted so a producer's attribution order
        survives the hop and two identical payloads serialise identically.
        """
        cleaned: list[str] = []
        for value in values:
            campaign_id = validate_uuid(value, field="campaign_id")
            if campaign_id not in cleaned:
                cleaned.append(campaign_id)
        return cleaned

    @model_validator(mode="after")
    def validate_span(self) -> MatchedKeywordRef:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not precede start_ms")
        if self.end_char is not None and self.end_char < self.start_char:
            raise ValueError("end_char must not precede start_char")
        return self

    @property
    def resolved_end_char(self) -> int:
        """``end_char``, or the legacy-message approximation from text length.

        Only a pre-change message can reach the fallback branch: the producer
        always sets ``end_char``.
        """
        if self.end_char is not None:
            return self.end_char
        return self.start_char + len(self.matched_text)

    def resolved_campaign_ids(self, job_campaign_ids: Sequence[str]) -> tuple[str, ...]:
        """Owning campaigns, falling back to the job's list for old messages.

        The fallback is deliberately the *old* broadening behaviour, because
        for a message serialised before per-match ownership existed that is the
        only information available -- and it is what the message meant when it
        was written.
        """
        if self.campaign_ids:
            return tuple(self.campaign_ids)
        return tuple(job_campaign_ids)


class TranscriptReference(MessageModel):
    """Transcripts are referenced, never inlined (ADR-003 size discipline)."""

    backend: Literal["sqlite"] = "sqlite"
    transcript_id: str

    @field_validator("transcript_id")
    @classmethod
    def validate_transcript_id(cls, value: str) -> str:
        return validate_uuid(value, field="transcript_id")


class AnalysisJobV1(MessageModel):
    """Matcher -> analysis worker. One job per confirmed conversation."""

    schema_name: Literal["radio.analysis.v1"] = Field(
        default=ANALYSIS_SCHEMA_V1, alias="schema"
    )
    analysis_job_id: str
    mention_id: str
    conversation_id: str
    station_id: str
    language: str | None = None
    transcript_reference: TranscriptReference
    matched_keywords: list[MatchedKeywordRef] = Field(
        default_factory=list, max_length=MAX_MATCHED_KEYWORDS
    )
    campaign_ids: list[str] = Field(default_factory=list, max_length=MAX_CAMPAIGN_IDS)
    truncated: bool = False
    trace_id: str
    created_at: datetime

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, frozen=True, populate_by_name=True
    )

    @field_validator("analysis_job_id", "mention_id", "conversation_id", "trace_id")
    @classmethod
    def validate_uuid_fields(cls, value: str) -> str:
        return validate_uuid(value)

    @field_validator("station_id")
    @classmethod
    def validate_station(cls, value: str) -> str:
        return validate_station_id(value)

    @field_validator("campaign_ids")
    @classmethod
    def validate_campaign_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            campaign_id = validate_uuid(value, field="campaign_id")
            if campaign_id not in cleaned:
                cleaned.append(campaign_id)
        if not cleaned:
            raise ValueError("An analysis job must map to at least one campaign")
        return cleaned

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().lower()
        if not code:
            return None
        if len(code) > 8 or not code.replace("-", "").isalnum():
            raise ValueError(f"Invalid language code: {value!r}")
        return code

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    def message_group_id(self) -> str:
        return self.station_id

    def deduplication_id(self) -> str:
        return self.analysis_job_id


# --- parsing -----------------------------------------------------------------


def _load_body(body: str) -> dict[str, Any]:
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise MessageTooLargeError("Received message exceeds the configured size ceiling")
    try:
        loaded = json.loads(body)
    except (TypeError, ValueError) as error:
        raise InvalidMessageError("Message body is not valid JSON", detail=str(error)[:200]) from error
    if not isinstance(loaded, dict):
        raise InvalidMessageError("Message body is not a JSON object")
    return loaded


def parse_transcription_job(body: str) -> TranscriptionJobV1:
    """Parse and validate a transcription message.

    Raises :class:`UnsupportedSchemaError` or :class:`InvalidMessageError`, both
    permanent — a malformed message will never become well-formed.
    """
    document = _load_body(body)
    declared = str(document.get("schema") or "")
    if declared not in SUPPORTED_TRANSCRIPTION_SCHEMAS:
        raise UnsupportedSchemaError(
            f"Unsupported transcription schema: {declared!r}",
            detail=f"supported={sorted(SUPPORTED_TRANSCRIPTION_SCHEMAS)}",
        )
    try:
        return TranscriptionJobV1.model_validate(document)
    except IdentifierError as error:
        raise InvalidMessageError("Transcription job has an invalid identifier", detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - any validation failure is permanent
        raise InvalidMessageError(
            "Transcription job failed schema validation", detail=str(error)[:1000]
        ) from error


def parse_analysis_job(body: str) -> AnalysisJobV1:
    """Parse and validate an analysis message."""
    document = _load_body(body)
    declared = str(document.get("schema") or "")
    if declared not in SUPPORTED_ANALYSIS_SCHEMAS:
        raise UnsupportedSchemaError(
            f"Unsupported analysis schema: {declared!r}",
            detail=f"supported={sorted(SUPPORTED_ANALYSIS_SCHEMAS)}",
        )
    try:
        return AnalysisJobV1.model_validate(document)
    except IdentifierError as error:
        raise InvalidMessageError("Analysis job has an invalid identifier", detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - any validation failure is permanent
        raise InvalidMessageError(
            "Analysis job failed schema validation", detail=str(error)[:1000]
        ) from error


__all__ = [
    "ANALYSIS_SCHEMA_V1",
    "AnalysisJobV1",
    "MAX_CAMPAIGN_IDS",
    "MAX_MATCHED_KEYWORDS",
    "MAX_MESSAGE_BYTES",
    "MatchedKeywordRef",
    "MessageModel",
    "SUPPORTED_ANALYSIS_SCHEMAS",
    "SUPPORTED_TRANSCRIPTION_SCHEMAS",
    "StorageDescriptor",
    "TRANSCRIPTION_SCHEMA_V1",
    "TranscriptReference",
    "TranscriptionJobV1",
    "parse_analysis_job",
    "parse_transcription_job",
]
