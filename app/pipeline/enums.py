"""Enumerated vocabulary shared by the pipeline, the database and the API.

These are `Literal` aliases rather than `enum.Enum` so they serialise directly
in Pydantic models and compare equal to the plain strings stored in SQLite,
matching the convention already used in `app/models.py`.
"""
from __future__ import annotations

from typing import Literal, get_args

# --- audio ------------------------------------------------------------------

AudioContentClass = Literal[
    "silence",
    "music",
    "singing",
    "speech",
    "speech_over_music",
    "jingle",
    "unknown",
]
AUDIO_CONTENT_CLASSES: tuple[str, ...] = get_args(AudioContentClass)

#: Classes whose audio is discarded from RAM and never written to the spool.
DISCARDABLE_AUDIO_CLASSES: frozenset[str] = frozenset({"silence", "music", "singing"})

#: Classes that are always transcribed. `unknown` is included on purpose:
#: recall is protected in stage 1 and precision is recovered from the
#: transcript in stage 2 (ADR-005, ADR-010).
TRANSCRIBABLE_AUDIO_CLASSES: frozenset[str] = frozenset(
    {"speech", "speech_over_music", "jingle", "unknown"}
)

# --- transcript-level content ------------------------------------------------

ContentType = Literal[
    "news",
    "interview",
    "advertisement",
    "announcement",
    "emergency_alert",
    "dj_commentary",
    "discussion",
    "station_identification",
    "song_lyrics",
    "unknown",
]
CONTENT_TYPES: tuple[str, ...] = get_args(ContentType)

#: content_type -> the campaign content-policy flag that governs it.
CONTENT_TYPE_POLICY_FLAG: dict[str, str] = {
    "news": "include_news",
    "interview": "include_interviews",
    "advertisement": "include_advertisements",
    "announcement": "include_announcements",
    "emergency_alert": "include_emergency_alerts",
    "dj_commentary": "include_dj_commentary",
    "discussion": "include_news",
    "station_identification": "include_announcements",
    "song_lyrics": "include_song_lyrics",
}

# --- keywords and matching ---------------------------------------------------

KeywordKind = Literal["brand", "person", "product", "organization", "topic", "concept", "other"]
KEYWORD_KINDS: tuple[str, ...] = get_args(KeywordKind)

#: Named entities: translated equivalents and broad concepts are never accepted.
#: Mirrors the existing rule in `services/llm.py::match_keyword`.
STRICT_ENTITY_KINDS: frozenset[str] = frozenset({"brand", "person", "product", "organization"})

AliasKind = Literal[
    "canonical",
    "native_script",
    "romanization",
    "translation",
    "abbreviation",
    "asr_variant",
    "phonetic",
]
ALIAS_KINDS: tuple[str, ...] = get_args(AliasKind)

MatchLevel = Literal[
    "exact",
    "alias",
    "transliteration",
    "fuzzy",
    "phonetic",
    "semantic",
]
MATCH_LEVELS: tuple[str, ...] = get_args(MatchLevel)

#: Levels that must survive a higher-quality pass-B re-decode before they may
#: become a confirmed mention (ADR-010 §5).
CONFIRMATION_REQUIRED_LEVELS: frozenset[str] = frozenset({"fuzzy", "phonetic", "semantic"})

# --- lifecycle ---------------------------------------------------------------

ConversationState = Literal["idle", "candidate", "open", "closing", "closed", "failed"]
CONVERSATION_STATES: tuple[str, ...] = get_args(ConversationState)

ConversationCloseReason = Literal[
    "silence",
    "music",
    "max_duration",
    "disconnect",
    "shutdown",
    "error",
]
CONVERSATION_CLOSE_REASONS: tuple[str, ...] = get_args(ConversationCloseReason)

JobStatus = Literal["pending", "running", "succeeded", "failed", "abandoned"]
JOB_STATUSES: tuple[str, ...] = get_args(JobStatus)

OutboxStatus = Literal["pending", "sending", "sent", "failed"]
OUTBOX_STATUSES: tuple[str, ...] = get_args(OutboxStatus)

InboxStatus = Literal["processing", "processed", "failed"]
INBOX_STATUSES: tuple[str, ...] = get_args(InboxStatus)

SubscriptionState = Literal[
    "desired",
    "pending_capacity",
    "starting",
    "active",
    "degraded",
    "winding_down",
    "stopped",
]
SUBSCRIPTION_STATES: tuple[str, ...] = get_args(SubscriptionState)

#: States in which a station occupies a compute slot.
ACTIVE_SUBSCRIPTION_STATES: frozenset[str] = frozenset(
    {"starting", "active", "degraded", "winding_down"}
)

WorkerRole = Literal[
    "api",
    "planner",
    "listener",
    "transcription",
    "analysis",
    "cleanup",
]
WORKER_ROLES: tuple[str, ...] = get_args(WorkerRole)

SpoolPressure = Literal["ok", "warning", "pause", "emergency"]
SPOOL_PRESSURES: tuple[str, ...] = get_args(SpoolPressure)

AsrPass = Literal["a", "b"]

Sentiment = Literal["positive", "neutral", "negative", "mixed"]
SENTIMENTS: tuple[str, ...] = get_args(Sentiment)

SpeakerStance = Literal[
    "supportive", "critical", "neutral", "promotional", "informative", "unclear"
]
SPEAKER_STANCES: tuple[str, ...] = get_args(SpeakerStance)

Urgency = Literal["low", "normal", "high", "critical"]
URGENCIES: tuple[str, ...] = get_args(Urgency)

EntityType = Literal["person", "organization", "product", "location", "event", "other"]
ENTITY_TYPES: tuple[str, ...] = get_args(EntityType)
