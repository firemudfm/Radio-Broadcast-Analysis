"""Identifier safety, deterministic sharding, heartbeats and log hygiene."""
from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
from app.observability import (
    JsonFormatter,
    TraceFilter,
    configure_logging,
    current_trace_id,
    log_fields,
    trace_context,
)
from app.pipeline.heartbeat import HeartbeatReader, HeartbeatWriter, StaleJobSweeper
from app.pipeline.ids import (
    IdentifierError,
    content_fingerprint,
    owns_station,
    stable_hash,
    stable_shard_index,
    validate_station_id,
    validate_uuid,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


# --- identifiers --------------------------------------------------------------


@pytest.mark.parametrize("value", ["rb-abc123", "hertz879", "A1", "a_b-c"])
def test_valid_station_ids_are_accepted(value: str) -> None:
    assert validate_station_id(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "..", "../etc", "a/b", "a\\b", "a b", "a;b", "a$b", "a" * 200, "-leading"],
)
def test_unsafe_station_ids_are_refused(value: str) -> None:
    with pytest.raises(IdentifierError):
        validate_station_id(value)


def test_station_id_length_matches_the_message_group_id_limit() -> None:
    assert validate_station_id("a" * 127)
    with pytest.raises(IdentifierError, match="MessageGroupId"):
        validate_station_id("a" * 129)


def test_uuid_validation_normalises_case() -> None:
    assert validate_uuid("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA").islower()
    with pytest.raises(IdentifierError):
        validate_uuid("not-a-uuid")


# --- deterministic sharding ---------------------------------------------------


def test_shard_index_is_stable_across_processes() -> None:
    """A regression guard against ever reaching for Python's hash().

    hash(str) is randomised by PYTHONHASHSEED, so two containers would disagree
    about station ownership: a silent split brain.
    """
    script = (
        "from app.pipeline.ids import stable_shard_index;"
        "print(stable_shard_index('rb-abc123', 8))"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": ".", "PATH": ""},
        )
        outputs.add(completed.stdout.strip())
    assert len(outputs) == 1, f"shard assignment is not process-stable: {outputs}"


def test_shard_index_matches_fixed_known_vectors() -> None:
    """Pinning the values makes an accidental hash-function change a failure."""
    assert stable_hash("rb-abc123") == 11_250_504_983_947_422_643
    assert stable_shard_index("rb-abc123", 1) == 0
    assert stable_shard_index("rb-abc123", 8) == 11_250_504_983_947_422_643 % 8


def test_single_shard_owns_every_station() -> None:
    for index in range(50):
        assert owns_station(f"rb-station{index}", shard_count=1, shard_index=0)


@pytest.mark.parametrize("shard_count", [2, 3, 4, 8])
def test_shards_partition_without_gaps_or_overlap(shard_count: int) -> None:
    stations = [f"rb-station{index}" for index in range(400)]
    owned: dict[str, int] = {}
    for shard_index in range(shard_count):
        for station in stations:
            if owns_station(station, shard_count=shard_count, shard_index=shard_index):
                assert station not in owned, "station owned by two shards"
                owned[station] = shard_index
    assert len(owned) == len(stations), "station owned by no shard"


def test_shard_index_must_be_inside_the_count() -> None:
    with pytest.raises(IdentifierError):
        owns_station("rb-a", shard_count=2, shard_index=2)


def test_content_fingerprint_is_order_sensitive_and_collision_resistant() -> None:
    assert content_fingerprint("a", "b") == content_fingerprint("a", "b")
    assert content_fingerprint("a", "b") != content_fingerprint("b", "a")
    # NUL separation: ("ab","c") must not collide with ("a","bc").
    assert content_fingerprint("ab", "c") != content_fingerprint("a", "bc")


# --- heartbeats ---------------------------------------------------------------


def test_heartbeat_round_trip(pipeline_database: Database) -> None:
    writer = HeartbeatWriter(
        pipeline_database, worker_id="listener-0-host", role="listener", shard_count=1
    )
    writer.beat(detail={"sessions": 3}, now=NOW)
    reader = HeartbeatReader(pipeline_database, stale_after_seconds=120)
    assert reader.role_status("listener", now=NOW) == "ok"
    assert reader.role_status("transcription", now=NOW) == "absent"


def test_stale_heartbeat_is_reported(pipeline_database: Database) -> None:
    writer = HeartbeatWriter(pipeline_database, worker_id="w1", role="listener")
    writer.beat(now=NOW - timedelta(seconds=600))
    reader = HeartbeatReader(pipeline_database, stale_after_seconds=120)
    assert reader.role_status("listener", now=NOW) == "stale"


def test_missing_shard_is_detected(pipeline_database: Database) -> None:
    """A missing shard silently drops its stations; health must say so."""
    HeartbeatWriter(
        pipeline_database, worker_id="w0", role="listener", shard_index=0, shard_count=3
    ).beat(now=NOW)
    reader = HeartbeatReader(pipeline_database, stale_after_seconds=120)
    coverage = reader.shard_coverage(now=NOW)
    assert coverage["missing"] == [1, 2]
    assert coverage["healthy"] is False


def test_duplicate_shard_is_detected(pipeline_database: Database) -> None:
    for worker in ("w0", "w0-clone"):
        HeartbeatWriter(
            pipeline_database, worker_id=worker, role="listener", shard_index=0, shard_count=2
        ).beat(now=NOW)
    HeartbeatWriter(
        pipeline_database, worker_id="w1", role="listener", shard_index=1, shard_count=2
    ).beat(now=NOW)
    coverage = HeartbeatReader(pipeline_database).shard_coverage(now=NOW)
    assert coverage["duplicated"] == [0]
    assert coverage["healthy"] is False


def test_full_shard_coverage_is_healthy(pipeline_database: Database) -> None:
    for index in range(3):
        HeartbeatWriter(
            pipeline_database,
            worker_id=f"w{index}",
            role="listener",
            shard_index=index,
            shard_count=3,
        ).beat(now=NOW)
    coverage = HeartbeatReader(pipeline_database).shard_coverage(now=NOW)
    assert coverage["healthy"] is True
    assert coverage["missing"] == []


# --- stale job sweeper --------------------------------------------------------


def _insert_job(database: Database, *, attempts: int, lease: datetime) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")

    def write(connection) -> None:
        # analysis_jobs.mention_id is a real foreign key, so the parent rows
        # must exist. That the test needs them is the constraint working.
        connection.execute(
            "INSERT INTO conversation_sessions(conversation_id, station_id, station_session_id,"
            " first_sequence_number, last_sequence_number, started_at_utc, trace_id,"
            " created_at_utc, updated_at_utc) VALUES ('c1','rb-abc','s1',1,2,?,?,?,?)",
            (stamp, "t", stamp, stamp),
        )
        connection.execute(
            "INSERT INTO mention_events(mention_id, conversation_id, station_id,"
            " broadcast_start_utc, trace_id, created_at_utc, updated_at_utc)"
            " VALUES ('m1','c1','rb-abc',?,?,?,?)",
            (stamp, "t", stamp, stamp),
        )
        connection.execute(
            "INSERT INTO analysis_jobs(analysis_job_id, mention_id, conversation_id, station_id,"
            " status, attempts, lease_expires_at_utc, trace_id, created_at_utc, updated_at_utc)"
            " VALUES ('j1','m1','c1','rb-abc','running',?,?,?,?,?)",
            (attempts, lease.isoformat().replace("+00:00", "Z"), "t", stamp, stamp),
        )

    database.write(write)


def test_expired_lease_returns_a_job_to_pending(pipeline_database: Database) -> None:
    _insert_job(pipeline_database, attempts=1, lease=NOW - timedelta(seconds=1))
    StaleJobSweeper(pipeline_database, max_attempts=5).sweep(now=NOW)
    row = pipeline_database.read_one("SELECT status FROM analysis_jobs WHERE analysis_job_id='j1'")
    assert str(row["status"]) == "pending"


def test_exhausted_job_is_abandoned_not_retried(pipeline_database: Database) -> None:
    _insert_job(pipeline_database, attempts=5, lease=NOW - timedelta(seconds=1))
    StaleJobSweeper(pipeline_database, max_attempts=5).sweep(now=NOW)
    row = pipeline_database.read_one(
        "SELECT status, last_error_code FROM analysis_jobs WHERE analysis_job_id='j1'"
    )
    assert str(row["status"]) == "abandoned"
    assert str(row["last_error_code"]) == "lease_exhausted"


def test_live_lease_is_left_alone(pipeline_database: Database) -> None:
    _insert_job(pipeline_database, attempts=1, lease=NOW + timedelta(seconds=300))
    StaleJobSweeper(pipeline_database).sweep(now=NOW)
    row = pipeline_database.read_one("SELECT status FROM analysis_jobs WHERE analysis_job_id='j1'")
    assert str(row["status"]) == "running"


# --- logging hygiene ----------------------------------------------------------


def test_json_formatter_emits_one_object_per_line() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    record.station_id = "rb-abc"  # type: ignore[attr-defined]
    record.segment_id = "seg-1"  # type: ignore[attr-defined]
    document = json.loads(JsonFormatter().format(record))
    assert document["message"] == "hello"
    assert document["station_id"] == "rb-abc"
    assert "\n" not in JsonFormatter().format(record)


def test_secrets_are_redacted_in_log_fields() -> None:
    fields = log_fields(
        station_id="rb-abc",
        stream_url="https://stream.example/live",
        aws_secret_access_key="AKIA-secret",
        presigned_url="https://s3/presigned",
        dropped=None,
    )
    assert fields["station_id"] == "rb-abc"
    assert fields["stream_url"] == "[redacted]"
    assert fields["aws_secret_access_key"] == "[redacted]"
    assert fields["presigned_url"] == "[redacted]"
    assert "dropped" not in fields


def test_json_formatter_redacts_secret_shaped_extras() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", (), None)
    record.stream_url = "https://stream.example/live"  # type: ignore[attr-defined]
    document = json.loads(JsonFormatter().format(record))
    assert document["stream_url"] == "[redacted]"


def test_exception_records_omit_the_traceback() -> None:
    """A traceback can carry filesystem paths and argument values."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    document = json.loads(JsonFormatter().format(record))
    assert document["error_type"] == "ValueError"
    assert document["error"] == "boom"
    assert "Traceback" not in json.dumps(document)


def test_trace_id_propagates_through_context() -> None:
    assert current_trace_id() is None
    with trace_context("trace-42"):
        assert current_trace_id() == "trace-42"
    assert current_trace_id() is None


def test_trace_filter_injects_the_ambient_id() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", (), None)
    with trace_context("trace-7"):
        TraceFilter().filter(record)
    assert record.trace_id == "trace-7"  # type: ignore[attr-defined]


def test_configure_logging_is_idempotent() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", log_format="json", stream=stream)
    configure_logging(level="INFO", log_format="json", stream=stream)
    assert len(logging.getLogger().handlers) == 1
    logging.getLogger("test").info("hello", extra={"station_id": "rb-abc"})
    assert json.loads(stream.getvalue().strip())["station_id"] == "rb-abc"
    configure_logging(level="INFO", log_format="text")


# --- orphaned pending jobs ----------------------------------------------------
#
# SQS FIFO retains a message for at most 4 days. A pending transcription row
# older than that has no message left to deliver: no worker will ever receive
# it, and the receive-side stale-skip can never fire. Production accumulated
# ~20k of these from a backlog that outlived retention.


def _insert_transcription_job(
    database: Database, *, segment_id: str, created: datetime, disposition: str = "pending"
) -> None:
    stamp = created.isoformat()

    def write(connection) -> None:
        connection.execute(
            "INSERT INTO station_sessions(station_session_id, station_id,"
            " generation, shard_index, status, started_at_utc)"
            " VALUES (?, 'st-1', 1, 0, 'streaming', ?)"
            " ON CONFLICT(station_session_id) DO NOTHING",
            (f"sess-{segment_id}", stamp),
        )
        connection.execute(
            "INSERT INTO audio_segments(segment_id, station_id,"
            " station_session_id, sequence_number, started_at_utc, ended_at_utc,"
            " duration_ms, content_class, storage_backend, storage_path, sha256,"
            " size_bytes, disposition, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?, 'st-1', ?, 1, ?, ?, 1000, 'speech', 'local', 'x.opus',"
            " 'd', 1, ?, 'tr', ?, ?)",
            (segment_id, f"sess-{segment_id}", stamp, stamp, disposition, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO transcription_jobs(segment_id, station_id, status,"
            " attempts, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?, 'st-1', 'pending', 0, 'tr', ?, ?)",
            (segment_id, stamp, stamp),
        )

    database.write(write)


def test_a_pending_job_older_than_retention_is_abandoned(
    pipeline_database: Database,
) -> None:
    _insert_transcription_job(
        pipeline_database, segment_id="seg-old", created=NOW - timedelta(hours=97)
    )
    results = StaleJobSweeper(pipeline_database).sweep(now=NOW)
    assert results["orphaned_pending"] == 1
    row = pipeline_database.read_one(
        "SELECT status, last_error_code FROM transcription_jobs WHERE segment_id='seg-old'"
    )
    assert str(row["status"]) == "abandoned"
    assert str(row["last_error_code"]) == "message_retention_expired"
    segment = pipeline_database.read_one(
        "SELECT disposition FROM audio_segments WHERE segment_id='seg-old'"
    )
    assert str(segment["disposition"]) == "disposable", "the audio is released to cleanup"


def test_a_younger_pending_job_is_left_for_the_queue(
    pipeline_database: Database,
) -> None:
    """Its message may still arrive; the receive-side skip owns that case."""
    _insert_transcription_job(
        pipeline_database, segment_id="seg-new", created=NOW - timedelta(hours=95)
    )
    results = StaleJobSweeper(pipeline_database).sweep(now=NOW)
    assert results["orphaned_pending"] == 0
    row = pipeline_database.read_one(
        "SELECT status FROM transcription_jobs WHERE segment_id='seg-new'"
    )
    assert str(row["status"]) == "pending"


def test_retained_evidence_survives_the_orphan_sweep(
    pipeline_database: Database,
) -> None:
    _insert_transcription_job(
        pipeline_database,
        segment_id="seg-kept",
        created=NOW - timedelta(hours=200),
        disposition="retained",
    )
    StaleJobSweeper(pipeline_database).sweep(now=NOW)
    segment = pipeline_database.read_one(
        "SELECT disposition FROM audio_segments WHERE segment_id='seg-kept'"
    )
    assert str(segment["disposition"]) == "retained", "evidence is never released by age"


def test_analysis_jobs_are_never_abandoned_by_age(pipeline_database: Database) -> None:
    """An analysis job is a mention waiting to exist."""
    _insert_job(pipeline_database, attempts=0, lease=NOW + timedelta(hours=1))
    pipeline_database.write(
        lambda connection: connection.execute(
            "UPDATE analysis_jobs SET status='pending', lease_expires_at_utc=NULL,"
            " created_at_utc=? WHERE analysis_job_id='j1'",
            ((NOW - timedelta(days=30)).isoformat(),),
        )
    )
    StaleJobSweeper(pipeline_database).sweep(now=NOW)
    row = pipeline_database.read_one(
        "SELECT status FROM analysis_jobs WHERE analysis_job_id='j1'"
    )
    assert str(row["status"]) == "pending"
