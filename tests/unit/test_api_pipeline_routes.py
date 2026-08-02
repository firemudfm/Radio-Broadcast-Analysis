"""API contract for the pipeline additions: /healthz, /readyz, monitoring.

The load-bearing assertion is backward compatibility: every field the current
frontend reads from ``/healthz`` must still be present with unchanged meaning,
and the new fields must be purely additive.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.config import Settings
from app.db import Database
from app.pipeline.heartbeat import HeartbeatWriter
from app.services.pipeline_status import REQUIRED_ROLES, PipelineStatusService

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)

#: Fields the current frontend reads. Removing or repurposing any of them is a
#: breaking change, so they are asserted explicitly rather than by shape.
EXISTING_HEALTH_FIELDS = frozenset(
    {
        "status",
        "database",
        "s3",
        "llm",
        "sync_enabled",
        "analysis_worker_enabled",
        "auth_mode",
        "storage_mode",
        "version",
    }
)


class StubS3:
    def list_objects_v2(self, **_kwargs):
        return {"KeyCount": 0}


class StubLlm:
    def health(self) -> bool:
        return True


class StubSpool:
    def __init__(self, percent: float = 5.0) -> None:
        self.percent = percent

    def usage_percent(self) -> float:
        return self.percent


def build_app(settings: Settings, database: Database, *, spool_percent: float = 5.0) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    app.state.database = database
    app.state.s3_client = StubS3()
    app.state.llm_client = StubLlm()
    app.state.pipeline_status_service = PipelineStatusService(
        settings, database, segment_store=StubSpool(spool_percent), clock=lambda: NOW
    )
    return app


@pytest.fixture
def base_settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_SYNC_ENABLED=False,
    )


@pytest.fixture
def shared_settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_SYNC_ENABLED=False,
        RADIO_QUEUE_BACKEND="memory",
    )


@pytest.fixture
def database(base_settings: Settings) -> Database:
    db = Database(base_settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


def beat_all(database: Database) -> None:
    for role in REQUIRED_ROLES:
        HeartbeatWriter(
            database, worker_id=f"{role}-0", role=role, pipeline_mode="shared_sqs"
        ).beat(now=NOW)


# --- backward compatibility ---------------------------------------------------


def test_healthz_keeps_every_existing_field(base_settings: Settings, database: Database) -> None:
    # Heartbeats first: with one pipeline, a deployment whose workers are absent
    # is legitimately `degraded`. This test is about the FIELD SHAPE the frontend
    # consumes, so it asks a healthy deployment.
    beat_all(database)
    with TestClient(build_app(base_settings, database)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert EXISTING_HEALTH_FIELDS <= set(body), "an existing client must not break"
    assert body["auth_mode"] == "none", "the pilot stays unauthenticated"
    assert body["storage_mode"] == "sqlite"
    assert body["status"] == "ok"


def test_healthz_always_reports_the_shared_pipeline(
    base_settings: Settings, database: Database
) -> None:
    """There is one pipeline, so the block is never absent -- a missing block
    used to mean `legacy`, and nothing should be able to mean that now."""
    with TestClient(build_app(base_settings, database)) as client:
        body = client.get("/healthz").json()
    assert body["pipeline_mode"] == "shared_sqs"
    assert body["pipeline"] is not None


def test_healthz_includes_the_pipeline_block(
    shared_settings: Settings, database: Database
) -> None:
    beat_all(database)
    with TestClient(build_app(shared_settings, database)) as client:
        body = client.get("/healthz").json()
    assert body["pipeline_mode"] == "shared_sqs"
    assert body["pipeline"]["unique_active_station_count"] == 0
    assert body["pipeline"]["components"]["listener"] == "ok"
    assert EXISTING_HEALTH_FIELDS <= set(body)


def test_a_dead_worker_degrades_health(
    shared_settings: Settings, database: Database
) -> None:
    with TestClient(build_app(shared_settings, database)) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "degraded", "no workers running is not healthy"
    assert body["database"] == "ok", "component fields keep their own meaning"


# --- readiness ----------------------------------------------------------------


def test_readyz_is_not_ready_without_workers(
    base_settings: Settings, database: Database
) -> None:
    """Readiness requires every worker role. A database-only "ready" was the
    legacy answer, and reporting it now would claim a pipeline that is capturing
    audio nobody transcribes."""
    with TestClient(build_app(base_settings, database)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["pipeline_mode"] == "shared_sqs"


def test_readyz_returns_503_when_workers_are_missing(
    shared_settings: Settings, database: Database
) -> None:
    with TestClient(build_app(shared_settings, database)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503, "a probe must be able to act without parsing"
    assert response.json()["ready"] is False


def test_readyz_becomes_ready_once_workers_report(
    shared_settings: Settings, database: Database
) -> None:
    beat_all(database)
    with TestClient(build_app(shared_settings, database)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_readyz_fails_when_the_spool_is_full(
    shared_settings: Settings, database: Database
) -> None:
    beat_all(database)
    app = build_app(shared_settings, database, spool_percent=97.0)
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["spool"] == "emergency"


# --- monitoring ---------------------------------------------------------------


def test_pipeline_monitoring_exposes_the_documented_counters(
    shared_settings: Settings, database: Database
) -> None:
    beat_all(database)
    with TestClient(build_app(shared_settings, database)) as client:
        response = client.get("/api/v1/monitoring/pipeline")
    assert response.status_code == 200
    body = response.json()
    for field in (
        "catalog_station_count",
        "campaign_station_reference_count",
        "unique_requested_station_count",
        "unique_active_station_count",
        "pending_capacity_station_count",
        "reused_station_stream_count",
        "worker_count",
        "queue_age_seconds",
        "spool_usage_percent",
        "listener_heartbeat",
        "transcription_worker_heartbeat",
        "analysis_worker_heartbeat",
    ):
        assert field in body, field
    assert body["queue_backend"] == "memory"
    assert body["segment_store"] == "local"


def test_monitoring_never_exposes_queue_urls_or_secrets(
    shared_settings: Settings, database: Database
) -> None:
    beat_all(database)
    with TestClient(build_app(shared_settings, database)) as client:
        raw = client.get("/api/v1/monitoring/pipeline").text.lower()
    for forbidden in ("secret", "aws_access", "sqs.", "amazonaws.com", "password"):
        assert forbidden not in raw, forbidden


def test_health_does_not_call_the_llm_for_generation(
    shared_settings: Settings, database: Database
) -> None:
    """Health may probe /health on the LLM; it must never generate."""

    class RecordingLlm:
        def __init__(self) -> None:
            self.health_calls = 0

        def health(self) -> bool:
            self.health_calls += 1
            return True

        def analyze(self, *_args, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("health must not run LLM generation")

    app = build_app(shared_settings, database)
    llm = RecordingLlm()
    app.state.llm_client = llm
    with TestClient(app) as client:
        client.get("/healthz")
    assert llm.health_calls == 1
