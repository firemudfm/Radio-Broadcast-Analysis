"""Pydantic models for the v0.4 radio catalogue, monitoring, and admission API.

Public catalogue responses never contain raw or resolved stream URLs; the
browser only ever handles station UUIDs and FastAPI-issued preview tokens.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiModel(BaseModel):
    """Same strict config as app.models.ApiModel; duplicated locally so this
    module stays a leaf and app.models can import from it without a cycle."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

MonitoringStatus = Literal[
    "available",
    "pending_probe",
    "probing",
    "pending_capacity",
    "activating",
    "active",
    "degraded",
    "failed_probe",
    "stopping",
    "stopped",
]

StationJobAction = Literal["probe", "activate", "stop", "reprobe"]
StationJobStatus = Literal["pending", "running", "completed", "failed"]
SelectionMode = Literal["explicit", "country_top", "country_all"]


class RadioCountry(ApiModel):
    code: str
    name: str
    station_count: int = 0


class RadioLanguage(ApiModel):
    code: str
    name: str
    station_count: int = 0


class RadioNamedOption(ApiModel):
    """Tag or codec option."""

    name: str
    station_count: int = 0


class CatalogStation(ApiModel):
    station_uuid: str
    name: str
    country_code: str | None = None
    country_name: str | None = None
    state: str | None = None
    iso_3166_2: str | None = None
    language_codes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    favicon_url: str | None = None
    homepage_url: str | None = None
    codec: str | None = None
    bitrate_kbps: int | None = None
    is_hls: bool = False
    radio_browser_healthy: bool | None = None
    votes: int = 0
    click_count: int = 0
    source: Literal["radio-browser", "curated-overlay", "radio-browser+overlay"] = "radio-browser"
    monitoring_status: MonitoringStatus = "available"
    managed_station_id: int | None = None
    probe_status: str | None = None
    active_campaign_count: int = 0


class CatalogStationPage(ApiModel):
    items: list[CatalogStation]
    offset: int = 0
    limit: int = 50
    has_more: bool = False
    source: str = "radio-browser"
    mirror: str | None = None
    cache_age_seconds: int = 0


class CountryListResponse(ApiModel):
    items: list[RadioCountry]


class LanguageListResponse(ApiModel):
    items: list[RadioLanguage]


class NamedOptionListResponse(ApiModel):
    items: list[RadioNamedOption]


class StationSelectionFilters(ApiModel):
    language: str | None = None
    tags: list[str] = Field(default_factory=list)
    codec: str | None = None
    healthy_only: bool = True
    https_only: bool = False
    bitrate_min: int | None = Field(default=None, ge=0, le=2048)
    bitrate_max: int | None = Field(default=None, ge=0, le=2048)


class StationSelection(ApiModel):
    mode: SelectionMode
    station_uuids: list[str] = Field(default_factory=list, max_length=100)
    country_codes: list[str] = Field(default_factory=list, max_length=10)
    maximum_stations: int = Field(default=5, ge=1, le=50)
    filters: StationSelectionFilters = Field(default_factory=StationSelectionFilters)

    @field_validator("station_uuids")
    @classmethod
    def clean_uuids(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip().lower()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                output.append(cleaned)
        return output

    @field_validator("country_codes")
    @classmethod
    def clean_codes(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        for value in values:
            cleaned = value.strip().upper()
            if len(cleaned) != 2 or not cleaned.isalpha():
                raise ValueError(f"Invalid country code: {value!r}")
            if cleaned not in output:
                output.append(cleaned)
        return output

    @model_validator(mode="after")
    def check_mode_inputs(self) -> StationSelection:
        if self.mode == "explicit" and not self.station_uuids:
            raise ValueError("explicit selection requires station_uuids")
        if self.mode in {"country_top", "country_all"} and not self.country_codes:
            raise ValueError(f"{self.mode} selection requires country_codes")
        return self


class ManagedStationView(ApiModel):
    id: int
    station_uuid: str
    local_station_id: str | None = None
    name: str
    country_code: str | None = None
    state: str | None = None
    language_codes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    favicon_url: str | None = None
    homepage_url: str | None = None
    codec: str | None = None
    bitrate_kbps: int | None = None
    is_hls: bool = False
    desired_state: str
    actual_state: MonitoringStatus
    probe_status: str | None = None
    probe_checked_at_utc: datetime | None = None
    last_error: str | None = None
    active_campaign_count: int = 0
    legacy_pinned: bool = False
    stop_after_utc: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ManagedStationListResponse(ApiModel):
    items: list[ManagedStationView]


class StationProbeResultView(ApiModel):
    managed_station_id: int
    station_uuid: str
    status: str
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration_seconds: float | None = None
    error: str | None = None
    checked_at_utc: datetime | None = None


class StationJobView(ApiModel):
    id: int
    managed_station_id: int
    action: StationJobAction
    status: StationJobStatus
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class CapacityView(ApiModel):
    vcpus: int
    memory_total_gib: float
    memory_available_gib: float | None = None
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    active_stations: int
    active_station_limit: int
    # Additive. The active limit is compute; this is the control-plane bound on
    # how many DISTINCT stations campaigns may request. Existing clients that
    # ignore it are unaffected.
    requested_station_limit: int = 1000
    pending_probe: int = 0
    pending_capacity: int = 0
    oldest_processing_job_seconds: int | None = None
    can_add_station: bool
    reason: str


class StationEstimateRequest(ApiModel):
    station_selection: StationSelection


class StationEstimateView(ApiModel):
    mode: SelectionMode
    matched_stations: int
    selected_stations: int
    already_active: int
    can_start_now: int
    pending_probe: int
    pending_capacity: int
    failed: int
    active_station_limit: int
    # Additive. The active limit is compute; this is the control-plane bound on
    # how many DISTINCT stations campaigns may request. Existing clients that
    # ignore it are unaffected.
    requested_station_limit: int = 1000
    capacity_reason: str
    station_uuids_preview: list[str] = Field(default_factory=list, max_length=20)


class ActivateStationResponse(ApiModel):
    managed_station_id: int
    station_uuid: str
    actual_state: MonitoringStatus
    job_id: int | None = None
    detail: str


class StopStationResponse(ApiModel):
    managed_station_id: int
    actual_state: MonitoringStatus
    detail: str


class PreviewTokenResponse(ApiModel):
    url: str
    expires_at_utc: datetime
    max_seconds: int


class CampaignStationSummary(ApiModel):
    station_uuid: str | None = None
    station_id: str | None = None
    name: str
    monitoring_status: MonitoringStatus = "available"
    probe_status: str | None = None


class CampaignSelectionSummary(ApiModel):
    mode: SelectionMode | None = None
    selected_station_count: int = 0
    active_count: int = 0
    pending_probe_count: int = 0
    pending_capacity_count: int = 0
    failed_count: int = 0
    stations: list[CampaignStationSummary] = Field(default_factory=list)
