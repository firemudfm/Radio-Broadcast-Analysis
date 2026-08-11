"""Stale transcription jobs are skipped, not decoded.

The production incident: 19,857 messages queued on the transcription FIFO
because ASR decoded slower than the listener captured. Every queued segment
still cost a full decode when its turn came, so fresh audio waited behind
day-old audio and the backlog could only grow. Jobs past the freshness window
are now acknowledged without decoding.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.pipeline.contracts import StorageDescriptor, TranscriptionJobV1
from app.pipeline.queue import ReceivedMessage
from app.workers.transcription import TranscriptionWorker

STATION_ID = "rb-78012206-1aa1-11e9-a80b-52543be04c81"


class ExplodingDependency:
    """Proves the skip happens before any read or decode is paid."""

    def __getattr__(self, name):
        raise AssertionError(f"stale skip must not touch {name}")


def make_job(*, age_hours: float) -> TranscriptionJobV1:
    now = datetime.now(UTC)
    return TranscriptionJobV1(
        job_id=str(uuid4()),
        segment_id=str(uuid4()),
        station_id=STATION_ID,
        station_session_id=str(uuid4()),
        sequence_number=1,
        started_at=now - timedelta(hours=age_hours),
        duration_ms=20_000,
        content_class="speech",
        keyword_index_version=0,
        storage=StorageDescriptor(
            backend="local",
            path="x.opus",
            sha256="a" * 64,
            size_bytes=1000,
        ),
        trace_id=str(uuid4()),
        created_at=now - timedelta(hours=age_hours),
    )


def make_worker(settings, database) -> TranscriptionWorker:
    return TranscriptionWorker(
        settings,
        database,
        queue=object(),
        segment_store=ExplodingDependency(),
        transcription_service=ExplodingDependency(),
    )


def seed_segment_rows(database, job: TranscriptionJobV1) -> None:
    stamp = datetime.now(UTC).isoformat()

    def write(connection) -> None:
        connection.execute(
            "INSERT INTO audio_segments(segment_id, station_id,"
            " station_session_id, sequence_number, started_at_utc, ended_at_utc,"
            " duration_ms, content_class, storage_backend, storage_path, sha256,"
            " size_bytes, disposition, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,1,?,?,20000,'speech','local','x.opus',?,1000,"
            " 'pending',?,?,?)",
            (
                job.segment_id, STATION_ID, job.station_session_id, stamp, stamp,
                "a" * 64, job.trace_id, stamp, stamp,
            ),
        )
        connection.execute(
            "INSERT INTO transcription_jobs(segment_id, station_id, status,"
            " attempts, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?,?,'pending',0,?,?,?)",
            (job.segment_id, STATION_ID, job.trace_id, stamp, stamp),
        )

    database.write(write)


def message_for(job: TranscriptionJobV1) -> ReceivedMessage:
    return ReceivedMessage(
        message_id=str(uuid4()),
        receipt_handle="handle",
        body=job.to_body(),
        group_id=job.station_id,
        deduplication_id=job.segment_id,
    )


def test_a_stale_job_is_acknowledged_without_decoding(settings, database) -> None:
    worker = make_worker(settings, database)
    job = make_job(age_hours=settings.RADIO_TRANSCRIPTION_MAX_AGE_HOURS + 2)
    seed_segment_rows(database, job)

    outcome = worker._handle_job(job, message_for(job))

    assert outcome.handled is True
    assert outcome.result_reference == "stale-skip"
    assert worker.stats["stale_skipped"] == 1
    job_row = database.read_one(
        "SELECT status FROM transcription_jobs WHERE segment_id=?", (job.segment_id,)
    )
    assert job_row is not None and job_row["status"] == "abandoned"
    segment_row = database.read_one(
        "SELECT disposition FROM audio_segments WHERE segment_id=?", (job.segment_id,)
    )
    assert segment_row is not None and segment_row["disposition"] == "disposable"


def test_a_fresh_job_is_not_skipped(settings, database) -> None:
    """A fresh job proceeds into the real path, which here means touching the
    segment store; the exploding stand-in proves the skip did not fire."""
    import pytest

    worker = make_worker(settings, database)
    job = make_job(age_hours=0)
    with pytest.raises(AssertionError, match="must not touch read"):
        worker._handle_job(job, message_for(job))
    assert worker.stats["stale_skipped"] == 0
