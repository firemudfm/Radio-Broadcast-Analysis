"""Queue FIFO semantics, outbox durability and consumer idempotency.

The crash-window tests (ADR-009) are the point of this module: they encode the
two failure modes that a naive "commit then send" would produce.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
from app.pipeline.idempotency import InboxGuard, MessageProcessor, ProcessingOutcome
from app.pipeline.outbox import OutboxDispatcher, backoff_delay, enqueue
from app.pipeline.queue import MemoryQueue, ReceivedMessage
from app.pipeline.sqs_queue import SqsQueue

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


# --- FIFO semantics -----------------------------------------------------------


def test_ordering_is_preserved_within_a_group(memory_queue: MemoryQueue) -> None:
    for index in range(3):
        memory_queue.send(f"body-{index}", group_id="station-a", deduplication_id=f"d{index}")
    first = memory_queue.receive(max_messages=10)
    assert [m.body for m in first] == ["body-0"]
    memory_queue.delete(first[0].receipt_handle)
    second = memory_queue.receive(max_messages=10)
    assert [m.body for m in second] == ["body-1"]


def test_only_one_message_per_group_is_in_flight(memory_queue: MemoryQueue) -> None:
    """The constraint that shapes ADR-003 and the capacity default."""
    memory_queue.send("a1", group_id="station-a", deduplication_id="a1")
    memory_queue.send("a2", group_id="station-a", deduplication_id="a2")
    received = memory_queue.receive(max_messages=10)
    assert len(received) == 1
    assert memory_queue.receive(max_messages=10) == []


def test_different_groups_process_in_parallel(memory_queue: MemoryQueue) -> None:
    """Parallelism comes from many stations, not from racing one station."""
    memory_queue.send("a", group_id="station-a", deduplication_id="a")
    memory_queue.send("b", group_id="station-b", deduplication_id="b")
    memory_queue.send("c", group_id="station-c", deduplication_id="c")
    received = memory_queue.receive(max_messages=10)
    assert {m.group_id for m in received} == {"station-a", "station-b", "station-c"}


def test_deduplication_suppresses_a_repeat_send(memory_queue: MemoryQueue) -> None:
    memory_queue.send("body", group_id="g", deduplication_id="same")
    memory_queue.send("body", group_id="g", deduplication_id="same")
    assert memory_queue.approximate_depth() == 1


def test_deduplication_window_expires(fake_clock) -> None:
    """SQS deduplication lasts 5 minutes; the inbox is the real guarantee."""
    queue = MemoryQueue("q.fifo", visibility_seconds=30, clock=fake_clock)
    queue.send("body", group_id="g", deduplication_id="same")
    received = queue.receive()
    queue.delete(received[0].receipt_handle)
    fake_clock.advance(301)
    queue.send("body", group_id="g", deduplication_id="same")
    assert queue.approximate_depth() == 1


def test_visibility_expiry_redelivers(fake_clock) -> None:
    queue = MemoryQueue("q.fifo", visibility_seconds=30, clock=fake_clock)
    queue.send("body", group_id="g", deduplication_id="d")
    first = queue.receive()
    assert first[0].receive_count == 1
    assert queue.receive() == []
    fake_clock.advance(31)
    again = queue.receive()
    assert again[0].receive_count == 2


def test_visibility_extension_prevents_redelivery(fake_clock) -> None:
    queue = MemoryQueue("q.fifo", visibility_seconds=30, clock=fake_clock)
    queue.send("body", group_id="g", deduplication_id="d")
    received = queue.receive()[0]
    fake_clock.advance(25)
    queue.extend_visibility(received.receipt_handle, seconds=60)
    fake_clock.advance(20)
    assert queue.receive() == []


def test_delete_removes_the_message(memory_queue: MemoryQueue) -> None:
    memory_queue.send("body", group_id="g", deduplication_id="d")
    received = memory_queue.receive()[0]
    memory_queue.delete(received.receipt_handle)
    assert memory_queue.approximate_depth() == 0
    assert memory_queue.in_flight_count() == 0


def test_sqs_queue_refuses_a_non_fifo_url() -> None:
    with pytest.raises(ValueError, match="FIFO"):
        SqsQueue(object(), "https://sqs.eu-north-1.amazonaws.com/1/plain-queue")


def test_sqs_queue_sends_group_and_dedup_ids() -> None:
    class Spy:
        def __init__(self) -> None:
            self.kwargs: dict = {}

        def send_message(self, **kwargs):
            self.kwargs = kwargs
            return {"MessageId": "m-1"}

    spy = Spy()
    queue = SqsQueue(spy, "https://sqs.eu-north-1.amazonaws.com/1/radio-transcription.fifo")
    queue.send("body", group_id="rb-abc", deduplication_id="seg-1")
    assert spy.kwargs["MessageGroupId"] == "rb-abc"
    assert spy.kwargs["MessageDeduplicationId"] == "seg-1"


# --- outbox -------------------------------------------------------------------


def _enqueue(database: Database, *, dedup: str = "seg-1", queue_name: str = "t.fifo") -> None:
    database.write(
        lambda connection: enqueue(
            connection,
            queue_name=queue_name,
            message_group_id="rb-abc",
            message_deduplication_id=dedup,
            payload='{"schema":"radio.transcription.v1"}',
            trace_id="trace-1",
            now=NOW,
        )
    )


def test_enqueue_is_idempotent(pipeline_database: Database) -> None:
    _enqueue(pipeline_database)
    _enqueue(pipeline_database)
    rows = pipeline_database.read_all("SELECT count(*) AS n FROM outbox_events")
    assert int(rows[0]["n"]) == 1


def test_crash_before_send_is_recovered(pipeline_database: Database) -> None:
    """The silent-stall case: state committed, message never queued."""
    queue = MemoryQueue("t.fifo")
    _enqueue(pipeline_database)
    # The process "crashes" here — nothing was sent.
    assert queue.approximate_depth() == 0

    dispatcher = OutboxDispatcher(pipeline_database, {"t.fifo": queue}, clock=lambda: NOW)
    stats = dispatcher.dispatch_once()
    assert stats["sent"] == 1
    assert queue.approximate_depth() == 1


def test_crash_after_send_before_marking_sent_resends_safely(pipeline_database: Database) -> None:
    """A stale lease resends; SQS dedup and the consumer inbox absorb it."""
    queue = MemoryQueue("t.fifo")
    _enqueue(pipeline_database)

    class DyingQueue:
        def send(self, body, *, group_id, deduplication_id):
            queue.send(body, group_id=group_id, deduplication_id=deduplication_id)
            raise RuntimeError("process died after the send landed")

    dispatcher = OutboxDispatcher(
        pipeline_database, {"t.fifo": DyingQueue()}, clock=lambda: NOW, jitter=lambda: 1.0
    )
    dispatcher.dispatch_once()
    assert queue.approximate_depth() == 1

    later = OutboxDispatcher(
        pipeline_database, {"t.fifo": queue}, clock=lambda: NOW + timedelta(seconds=600)
    )
    stats = later.dispatch_once()
    assert stats["sent"] == 1
    # Deduplicated by the queue: exactly one business message survives.
    assert queue.approximate_depth() == 1


def test_stale_sending_lease_is_reclaimed(pipeline_database: Database) -> None:
    queue = MemoryQueue("t.fifo")
    _enqueue(pipeline_database)
    pipeline_database.write(
        lambda connection: connection.execute(
            "UPDATE outbox_events SET status='sending', lease_expires_at_utc=?",
            ((NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )
    )
    dispatcher = OutboxDispatcher(pipeline_database, {"t.fifo": queue}, clock=lambda: NOW)
    assert dispatcher.reclaim_stale_leases() == 1


def test_exhausted_attempts_fail_and_record(pipeline_database: Database) -> None:
    class BrokenQueue:
        def send(self, body, *, group_id, deduplication_id):
            raise RuntimeError("SQS is down")

    _enqueue(pipeline_database)
    dispatcher = OutboxDispatcher(
        pipeline_database,
        {"t.fifo": BrokenQueue()},
        max_attempts=1,
        clock=lambda: NOW,
        jitter=lambda: 0.0,
    )
    stats = dispatcher.dispatch_once()
    assert stats["failed"] == 1
    row = pipeline_database.read_one("SELECT status FROM outbox_events")
    assert str(row["status"]) == "failed"
    failure = pipeline_database.read_one(
        "SELECT error_code FROM processing_failures WHERE component='outbox'"
    )
    assert str(failure["error_code"]) == "outbox_exhausted"


def test_missing_queue_configuration_does_not_burn_attempts(pipeline_database: Database) -> None:
    _enqueue(pipeline_database, queue_name="unconfigured.fifo")
    dispatcher = OutboxDispatcher(
        pipeline_database, {}, clock=lambda: NOW, jitter=lambda: 0.0
    )
    stats = dispatcher.dispatch_once()
    assert stats["skipped"] == 1
    row = pipeline_database.read_one("SELECT status, attempts FROM outbox_events")
    assert str(row["status"]) == "pending"
    assert int(row["attempts"]) == 0


def test_backoff_grows_and_is_jittered() -> None:
    assert backoff_delay(0, jitter=lambda: 1.0) < backoff_delay(3, jitter=lambda: 1.0)
    assert backoff_delay(5, jitter=lambda: 0.0) == 0.0
    assert backoff_delay(999, jitter=lambda: 1.0) == backoff_delay(9, jitter=lambda: 1.0)


def test_prune_keeps_failed_rows(pipeline_database: Database) -> None:
    """Failed rows are evidence that work was lost; they are never auto-pruned."""
    _enqueue(pipeline_database, dedup="sent-1")
    _enqueue(pipeline_database, dedup="failed-1")
    old = (NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    pipeline_database.write(
        lambda connection: connection.execute(
            "UPDATE outbox_events SET status=CASE message_deduplication_id"
            " WHEN 'sent-1' THEN 'sent' ELSE 'failed' END, updated_at_utc=?",
            (old,),
        )
    )
    dispatcher = OutboxDispatcher(pipeline_database, {}, clock=lambda: NOW)
    assert dispatcher.prune(retention_days=7) == 1
    remaining = pipeline_database.read_all("SELECT status FROM outbox_events")
    assert [str(row["status"]) for row in remaining] == ["failed"]


def test_stats_report_backlog_depth_and_age(pipeline_database: Database) -> None:
    _enqueue(pipeline_database)
    dispatcher = OutboxDispatcher(
        pipeline_database, {}, clock=lambda: NOW + timedelta(seconds=45)
    )
    stats = dispatcher.stats()
    assert stats["pending"] == 1
    assert stats["oldest_pending_seconds"] == pytest.approx(45.0, abs=1.0)


# --- consumer inbox -----------------------------------------------------------


def _message(dedup: str = "seg-1", receipt: str = "r-1") -> ReceivedMessage:
    return ReceivedMessage(
        message_id="m-1",
        receipt_handle=receipt,
        body="{}",
        group_id="rb-abc",
        deduplication_id=dedup,
    )


def test_inbox_records_and_detects_a_processed_message(pipeline_database: Database) -> None:
    guard = InboxGuard(pipeline_database, "t.fifo")
    assert guard.already_processed("seg-1") is False
    pipeline_database.write(
        lambda connection: guard.record_processed(connection, _message(), now=NOW)
    )
    assert guard.already_processed("seg-1") is True


def test_duplicate_delivery_creates_no_second_business_row(
    pipeline_database: Database, fake_clock
) -> None:
    """The case SQS deduplication cannot cover.

    Its window is 5 minutes. A producer retry after a longer outage re-sends
    the same logical message and the queue accepts it. The inbox — not SQS — is
    what makes reprocessing a no-op.
    """
    queue = MemoryQueue("t.fifo", visibility_seconds=60, clock=fake_clock)
    queue.send("{}", group_id="rb-abc", deduplication_id="seg-1")
    processor = MessageProcessor(
        database=pipeline_database,
        queue=queue,
        queue_name="t.fifo",
        component="test",
        wait_seconds=0,
        heartbeat_seconds=3600,
    )
    calls = {"n": 0}

    def handler(message: ReceivedMessage) -> ProcessingOutcome:
        calls["n"] += 1

        def commit(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO processing_failures(component, error_code, retryable, message,"
                " created_at_utc) VALUES ('test','marker',0,'business row', ?)",
                (NOW.isoformat(),),
            )
            processor.inbox.record_processed(connection, message, now=NOW)

        pipeline_database.write(commit)
        return ProcessingOutcome(handled=True)

    assert processor.poll_once(handler)["processed"] == 1

    # 10-minute outage, then the producer retries: past the dedup window, so the
    # queue genuinely re-enqueues it.
    fake_clock.advance(600)
    queue.send("{}", group_id="rb-abc", deduplication_id="seg-1")
    assert queue.approximate_depth() == 1, "queue deduplication should have lapsed by now"

    stats = processor.poll_once(handler)
    assert stats["duplicate"] == 1
    assert calls["n"] == 1, "handler must not run a second time"
    rows = pipeline_database.read_all(
        "SELECT count(*) AS n FROM processing_failures WHERE error_code='marker'"
    )
    assert int(rows[0]["n"]) == 1
    assert queue.approximate_depth() == 0, "the duplicate must still be acknowledged"


def test_message_is_deleted_only_after_the_business_commit(pipeline_database: Database) -> None:
    """Ordering guard: a handler that never commits must not lose the message."""
    queue = MemoryQueue("t.fifo", visibility_seconds=60)
    queue.send("{}", group_id="rb-abc", deduplication_id="seg-1")
    processor = MessageProcessor(
        database=pipeline_database,
        queue=queue,
        queue_name="t.fifo",
        component="test",
        wait_seconds=0,
        heartbeat_seconds=3600,
    )

    def failing_handler(message: ReceivedMessage) -> ProcessingOutcome:
        raise RuntimeError("boom before commit")

    stats = processor.poll_once(failing_handler)
    assert stats["retried"] == 1
    assert queue.in_flight_count() == 1, "message must remain in flight, not deleted"
    assert processor.inbox.already_processed("seg-1") is False


def test_permanent_error_records_and_deletes(pipeline_database: Database) -> None:
    from app.pipeline.errors import InvalidMessageError

    queue = MemoryQueue("t.fifo", visibility_seconds=60)
    queue.send("{}", group_id="rb-abc", deduplication_id="seg-1")
    processor = MessageProcessor(
        database=pipeline_database,
        queue=queue,
        queue_name="t.fifo",
        component="test",
        wait_seconds=0,
        heartbeat_seconds=3600,
    )

    def handler(message: ReceivedMessage) -> ProcessingOutcome:
        raise InvalidMessageError("unparseable")

    stats = processor.poll_once(handler)
    assert stats["failed"] == 1
    assert queue.in_flight_count() == 0
    assert queue.approximate_depth() == 0
    row = pipeline_database.read_one(
        "SELECT error_code FROM processing_failures WHERE component='test'"
    )
    assert str(row["error_code"]) == "invalid_message"


def test_retryable_error_leaves_the_message_visible(pipeline_database: Database) -> None:
    from app.pipeline.errors import UpstreamUnavailableError

    queue = MemoryQueue("t.fifo", visibility_seconds=60)
    queue.send("{}", group_id="rb-abc", deduplication_id="seg-1")
    processor = MessageProcessor(
        database=pipeline_database,
        queue=queue,
        queue_name="t.fifo",
        component="test",
        wait_seconds=0,
        heartbeat_seconds=3600,
    )

    def handler(message: ReceivedMessage) -> ProcessingOutcome:
        raise UpstreamUnavailableError("LLM is down")

    assert processor.poll_once(handler)["retried"] == 1
    assert queue.in_flight_count() == 1


def test_inbox_prune_respects_retention(pipeline_database: Database) -> None:
    guard = InboxGuard(pipeline_database, "t.fifo")
    pipeline_database.write(
        lambda connection: guard.record_processed(
            connection, _message(), now=NOW - timedelta(days=30)
        )
    )
    assert guard.prune(retention_days=7, now=NOW) == 1
    assert guard.already_processed("seg-1") is False
