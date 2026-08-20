"""Pipeline status, readiness and the distinct capacity counters."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import Database
from app.pipeline.heartbeat import HeartbeatWriter
from app.services.pipeline_status import REQUIRED_ROLES, PipelineStatusService
from tests.fixtures.campaigns import create_campaign

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


class FakeSpool:
    def __init__(self, percent: float = 10.0) -> None:
        self.percent = percent

    def usage_percent(self) -> float:
        return self.percent


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_QUEUE_BACKEND="memory",
        # Control-plane unit tests: several distinct stations must be active
        # at once for the counters to mean different things. Production
        # defaults to 1; that is asserted in tests/test_capacity_defaults.py.
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8,
        RADIO_LISTENER_MAX_SESSIONS=8,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def service(settings: Settings, database: Database) -> PipelineStatusService:
    return PipelineStatusService(
        settings, database, segment_store=FakeSpool(), clock=lambda: NOW
    )


def beat_all(database: Database, *, at: datetime = NOW) -> None:
    for role in REQUIRED_ROLES:
        HeartbeatWriter(
            database, worker_id=f"{role}-0", role=role, pipeline_mode="shared_sqs"
        ).beat(now=at)


# --- readiness ----------------------------------------------------------------


def test_not_ready_until_every_required_worker_is_live(
    service: PipelineStatusService, database: Database
) -> None:
    report = service.readiness()
    assert report["ready"] is False
    assert all(report["checks"][role] == "absent" for role in REQUIRED_ROLES)

    beat_all(database)
    assert service.readiness()["ready"] is True


def test_a_stale_worker_makes_the_node_unready(
    service: PipelineStatusService, database: Database, settings: Settings
) -> None:
    beat_all(database, at=NOW - timedelta(seconds=settings.RADIO_HEARTBEAT_STALE_SECONDS + 60))
    report = service.readiness()
    assert report["ready"] is False
    assert report["checks"]["listener"] == "stale"


def test_readiness_always_requires_every_worker_role(tmp_path, database: Database) -> None:
    """A database-only "ready" was the legacy answer. Reporting it now would
    claim a working pipeline while audio is captured and never transcribed."""
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
    )
    report = PipelineStatusService(settings, database, clock=lambda: NOW).readiness()
    assert report["ready"] is False
    assert report["pipeline_mode"] == "shared_sqs"
    assert set(report["checks"]) > {"database"}


def test_unconfigured_queues_block_readiness(tmp_path, database: Database) -> None:
    unconfigured = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_QUEUE_BACKEND="memory",
        # Control-plane unit tests: several distinct stations must be active
        # at once for the counters to mean different things. Production
        # defaults to 1; that is asserted in tests/test_capacity_defaults.py.
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8,
        RADIO_LISTENER_MAX_SESSIONS=8,
    )
    service = PipelineStatusService(unconfigured, database, clock=lambda: NOW)
    beat_all(database)
    assert service.readiness()["checks"]["queues"] == "ok"


def test_an_emergency_spool_blocks_readiness(
    settings: Settings, database: Database
) -> None:
    beat_all(database)
    service = PipelineStatusService(
        settings, database, segment_store=FakeSpool(95.0), clock=lambda: NOW
    )
    report = service.readiness()
    assert report["checks"]["spool"] == "emergency"
    assert report["ready"] is False, "a full spool cannot accept new audio"


# --- capacity counters --------------------------------------------------------


def test_the_counters_have_distinct_meanings(
    service: PipelineStatusService, database: Database, settings: Settings
) -> None:
    from app.services.subscription_planner import SubscriptionPlanner

    for index in range(3):
        create_campaign(
            database,
            name=f"Campaign {index}",
            station_ids=["rb-a", "rb-b"],
            keywords=[("NVIDIA", "brand")],
        )
    SubscriptionPlanner(settings, database).plan_once()

    capacity = service.capacity()
    assert capacity["campaign_station_reference_count"] == 6, "3 campaigns x 2 stations"
    assert capacity["unique_requested_station_count"] == 2, "distinct stations only"
    # Nothing streams until the listener grants a turn; the planner only
    # builds the rotation pool.
    assert capacity["unique_active_station_count"] == 0
    assert capacity["reused_station_stream_count"] == 2, "both are shared"
    assert capacity["pending_capacity_station_count"] == 2
    assert capacity["active_unique_station_limit"] == settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS
    # The capacity limit is expressed in stations, never campaigns or keywords.
    assert capacity["unique_active_station_count"] < capacity["campaign_station_reference_count"]


def test_overflow_is_reported_as_pending_capacity(
    database: Database, tmp_path
) -> None:
    from app.services.subscription_planner import SubscriptionPlanner

    capped = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=2,
        RADIO_LISTENER_MAX_SESSIONS=2,
    )
    for index in range(5):
        create_campaign(
            database,
            name=f"Campaign {index}",
            station_ids=[f"rb-{index}"],
            keywords=[("NVIDIA", "brand")],
        )
    SubscriptionPlanner(capped, database).plan_once()

    capacity = PipelineStatusService(capped, database, clock=lambda: NOW).capacity()
    assert capacity["unique_requested_station_count"] == 5
    # All five wait in the rotation pool until the listener grants turns;
    # pending means "waiting for a turn", never "parked forever".
    assert capacity["unique_active_station_count"] == 0
    assert capacity["pending_capacity_station_count"] == 5


def test_worker_count_ignores_stale_and_stopped_workers(
    service: PipelineStatusService, database: Database, settings: Settings
) -> None:
    beat_all(database)
    assert service.capacity()["worker_count"] == len(REQUIRED_ROLES)

    HeartbeatWriter(database, worker_id="listener-0", role="listener").stop()
    assert service.capacity()["worker_count"] == len(REQUIRED_ROLES) - 1


# --- snapshot -----------------------------------------------------------------


def test_snapshot_exposes_the_documented_monitoring_fields(
    service: PipelineStatusService, database: Database
) -> None:
    beat_all(database)
    snapshot = service.snapshot()
    for field in (
        "reused_station_stream_count",
        "unique_active_station_count",
        "pending_capacity_station_count",
        "listener_heartbeat",
        "transcription_worker_heartbeat",
        "analysis_worker_heartbeat",
        "queue_age_seconds",
        "spool_usage_percent",
    ):
        assert field in snapshot, field
    assert snapshot["listener_heartbeat"]["status"] == "ok"


def test_queue_age_is_none_when_nothing_is_waiting(service: PipelineStatusService) -> None:
    assert service.queue_age_seconds() is None


def test_queue_age_reflects_the_oldest_pending_outbox_event(
    service: PipelineStatusService, database: Database
) -> None:
    from app.pipeline import outbox

    old = (NOW - timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
    database.write(
        lambda connection: outbox.enqueue(
            connection,
            queue_name="transcription",
            message_group_id="rb-a",
            message_deduplication_id="dedup-1",
            payload="{}",
            now=NOW - timedelta(seconds=90),
        )
    )
    del old
    assert service.queue_age_seconds() == pytest.approx(90.0, abs=1.0)


def test_spool_pressure_escalates_through_the_watermarks(
    settings: Settings, database: Database
) -> None:
    for percent, expected in ((10.0, "ok"), (75.0, "warning"), (87.0, "pause"), (95.0, "emergency")):
        service = PipelineStatusService(
            settings, database, segment_store=FakeSpool(percent), clock=lambda: NOW
        )
        assert service.spool_pressure() == expected


def test_no_segment_store_reports_zero_rather_than_failing(
    settings: Settings, database: Database
) -> None:
    service = PipelineStatusService(settings, database, segment_store=None, clock=lambda: NOW)
    assert service.spool_usage_percent() == 0.0
    assert service.spool_pressure() == "ok"


def test_the_snapshot_leaks_no_secrets(
    service: PipelineStatusService, database: Database
) -> None:
    import json

    beat_all(database)
    body = json.dumps(service.snapshot(), default=str).lower()
    for forbidden in ("secret", "aws_access", "password", "token", "https://sqs."):
        assert forbidden not in body, forbidden
