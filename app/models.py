from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CampaignStatus = Literal["active", "paused"]
SentimentLabel = Literal["positive", "neutral", "negative", "mixed"]
MatchMode = Literal["tokens", "substring"]
KeywordType = Literal["brand", "person", "product", "organization", "topic", "concept", "other"]
AnalysisStatus = Literal["ready", "fallback", "pending", "disabled", "error"]
LlmHealth = Literal["ok", "error", "disabled"]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StationView(ApiModel):
    id: str
    name: str
    country_code: str | None = None
    language_codes: list[str] = Field(default_factory=list)
    connected: bool = True
    enabled: bool = True


class KeywordInput(ApiModel):
    value: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=25)
    match_mode: MatchMode = "tokens"
    keyword_type: KeywordType = "brand"
    semantic_matching: bool | None = None
    semantic_threshold: float = Field(default=0.74, ge=0.55, le=0.95)

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            marker = cleaned.casefold()
            if not cleaned or marker in seen:
                continue
            seen.add(marker)
            output.append(cleaned)
        return output

    @model_validator(mode="after")
    def set_semantic_default(self) -> KeywordInput:
        # Cross-language meaning matching is useful for concepts/topics, while
        # brand/person/product names remain exact/alias-first unless explicitly enabled.
        if self.semantic_matching is None:
            self.semantic_matching = self.keyword_type in {"topic", "concept"}
        return self


class KeywordView(ApiModel):
    id: str
    entity_id: str
    value: str
    aliases: list[str]
    match_mode: MatchMode
    keyword_type: KeywordType = "brand"
    semantic_matching: bool = False
    semantic_threshold: float = Field(default=0.74, ge=0.55, le=0.95)
    enabled: bool = True


class CampaignCreate(ApiModel):
    name: str = Field(min_length=2, max_length=120)
    objective: str = Field(default="brand_mentions", min_length=2, max_length=80)
    business_name: str | None = Field(default=None, max_length=160)
    business_description: str | None = Field(default=None, max_length=1000)
    keywords: list[KeywordInput] = Field(min_length=1, max_length=50)
    # Legacy pre-v0.4 contract: pipeline station ids (e.g. "hertz879"). Still
    # accepted. New callers send station_selection instead.
    station_ids: list[str] = Field(default_factory=list, max_length=100)
    station_selection: StationSelection | None = None
    backfill_days: int = Field(default=7, ge=0, le=14)

    @field_validator("station_ids")
    @classmethod
    def unique_station_ids(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            output.append(cleaned)
        return output

    @model_validator(mode="after")
    def require_station_source(self) -> CampaignCreate:
        if not self.station_ids and self.station_selection is None:
            raise ValueError("Provide station_ids (legacy) or station_selection")
        return self

    @field_validator("keywords")
    @classmethod
    def unique_keywords(cls, values: list[KeywordInput]) -> list[KeywordInput]:
        output: list[KeywordInput] = []
        seen: set[str] = set()
        for keyword in values:
            marker = keyword.value.casefold().strip()
            if not marker or marker in seen:
                continue
            seen.add(marker)
            output.append(keyword)
        if not output:
            raise ValueError("At least one keyword is required")
        return output


class CampaignUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    objective: str | None = Field(default=None, min_length=2, max_length=80)
    business_name: str | None = Field(default=None, max_length=160)
    business_description: str | None = Field(default=None, max_length=1000)
    keywords: list[KeywordInput] | None = Field(default=None, min_length=1, max_length=50)
    station_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    status: CampaignStatus | None = None

    @field_validator("station_ids")
    @classmethod
    def clean_station_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned = CampaignCreate.unique_station_ids(values)
        if not cleaned:
            raise ValueError("At least one station is required")
        return cleaned

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, values: list[KeywordInput] | None) -> list[KeywordInput] | None:
        return CampaignCreate.unique_keywords(values) if values is not None else None


class CampaignView(ApiModel):
    id: str
    name: str
    objective: str
    business_name: str | None
    business_description: str | None
    status: CampaignStatus
    monitor_from_utc: datetime
    stations: list[StationView]
    keywords: list[KeywordView]
    mentions_7d: int
    selection: CampaignSelectionSummary | None = None
    created_at: datetime
    updated_at: datetime


class SentimentView(ApiModel):
    label: SentimentLabel
    score: float | None = None
    margin: float | None = None
    needs_review: bool = False


class MentionView(ApiModel):
    id: str
    campaign_id: str
    campaign_name: str
    keyword: str
    matched_alias: str | None
    station: StationView
    context: str
    detected_language: str | None
    language_probability: float | None
    sentiment: SentimentView
    broadcast_start_utc: datetime
    broadcast_end_utc: datetime | None
    audio_duration_seconds: float | None
    playback_start_seconds: float
    playback_end_seconds: float | None
    audio_available: bool


class TranscriptWordView(ApiModel):
    text: str
    start_char: int
    end_char: int
    broadcast_start_utc: datetime | None = None
    broadcast_end_utc: datetime | None = None
    probability: float | None = None


class TranscriptSegmentView(ApiModel):
    id: str
    text: str
    start_char: int
    end_char: int
    broadcast_start_utc: datetime | None = None
    broadcast_end_utc: datetime | None = None
    detected_language: str | None = None
    source_transcript_key: str


class HighlightView(ApiModel):
    start_char: int
    end_char: int
    text: str
    keyword: str
    matched_alias: str | None = None
    method: Literal["timestamp", "exact", "normalized", "semantic"]
    broadcast_start_utc: datetime | None = None
    broadcast_end_utc: datetime | None = None


class LlmAnalysisView(ApiModel):
    status: AnalysisStatus
    model: str | None = None
    summary: str | None = None
    why_relevant: str | None = None
    speaker_intent: str | None = None
    sentiment: SentimentLabel | None = None
    target_relevance: Literal["direct", "indirect", "incidental", "not_relevant"] | None = None
    key_points: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    needs_review: bool = False
    generated_at_utc: datetime | None = None
    error: str | None = None


class MentionDetailView(ApiModel):
    mention: MentionView
    full_transcript: str
    highlighted_sentence: str | None = None
    transcript_segments: list[TranscriptSegmentView]
    words: list[TranscriptWordView]
    highlights: list[HighlightView]
    transcript_source_keys: list[str]
    analysis: LlmAnalysisView


class SentimentSummary(ApiModel):
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    needs_review: int = 0


class DashboardView(ApiModel):
    campaigns: list[CampaignView]
    mentions: list[MentionView]
    sentiment: SentimentSummary
    total_mentions: int
    generated_at_utc: datetime
    mention_window_days: int = 7
    auth_mode: Literal["none"] = "none"
    storage_mode: Literal["sqlite"] = "sqlite"


class StationListResponse(ApiModel):
    stations: list[StationView]


class CampaignListResponse(ApiModel):
    campaigns: list[CampaignView]


class MentionListResponse(ApiModel):
    mentions: list[MentionView]
    total: int


class AudioTokenResponse(ApiModel):
    url: str
    expires_at_utc: datetime


class SyncView(ApiModel):
    objects_scanned: int
    objects_loaded: int
    mentions_seen: int
    mentions_materialized: int
    completed_at_utc: datetime


class RuntimeView(ApiModel):
    api_version: str
    llm_enabled: bool
    llm_health: LlmHealth
    llm_model: str | None
    analysis_worker_enabled: bool
    analysis_pending: int
    analysis_ready: int
    analysis_errors: int
    semantic_discovery_enabled: bool
    semantic_matched: int
    semantic_not_matched: int
    semantic_errors: int


class HealthView(ApiModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    s3: Literal["ok", "error"]
    llm: LlmHealth
    sync_enabled: bool
    analysis_worker_enabled: bool
    auth_mode: Literal["none"] = "none"
    storage_mode: Literal["sqlite"] = "sqlite"
    version: str
    # v0.5, additive. Absent in legacy deployments, so existing clients that
    # ignore unknown fields are unaffected and no field changes meaning.
    pipeline_mode: Literal["legacy", "shared_sqs"] = "legacy"
    pipeline: dict[str, Any] | None = None


class ReadinessView(ApiModel):
    """Whether this node can do its job right now, as opposed to being alive."""

    ready: bool
    pipeline_mode: Literal["legacy", "shared_sqs"]
    checks: dict[str, str]


class PipelineStatusView(ApiModel):
    """Shared-pipeline capacity and worker liveness."""

    pipeline_mode: Literal["legacy", "shared_sqs"]
    queue_backend: str
    segment_store: str
    queues_configured: bool
    components: dict[str, str]
    catalog_station_count: int
    campaign_station_reference_count: int
    unique_requested_station_count: int
    unique_active_station_count: int
    pending_capacity_station_count: int
    reused_station_stream_count: int
    active_unique_station_limit: int
    worker_count: int
    listener_shard_count: int
    queue_age_seconds: float | None = None
    spool_usage_percent: float = 0.0
    spool_pressure: str = "ok"
    listener_heartbeat: dict[str, Any] | None = None
    #: Live listening turns: the observable proof that rotation is working.
    listener_sessions: list[dict[str, Any]] = Field(default_factory=list)
    transcription_worker_heartbeat: dict[str, Any] | None = None
    analysis_worker_heartbeat: dict[str, Any] | None = None
    planner_heartbeat: dict[str, Any] | None = None
    outbox: dict[str, int] = Field(default_factory=dict)
    shard_coverage: dict[str, Any] = Field(default_factory=dict)


# v0.4 forward references (kept at the bottom to avoid an import cycle).
from .models_catalog import CampaignSelectionSummary, StationSelection  # noqa: E402

CampaignCreate.model_rebuild()
CampaignView.model_rebuild()
