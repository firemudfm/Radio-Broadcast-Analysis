"""Consumer inbox and the message-processing loop (ADR-009).

The ordering rule this module exists to enforce:

    commit the business result AND the inbox row in ONE transaction,
    and only THEN delete the SQS message.

Never the other way round. If the process dies between the commit and the
delete, the message is redelivered and the inbox turns it into a no-op. If it
dies before the commit, the work is redone — which is why every business insert
is guarded by a UNIQUE constraint rather than a prior SELECT.

SQS FIFO deduplication is defence in depth only: its window is **5 minutes**
(verified against the SQS Developer Guide). A retry after a longer outage would
duplicate. This table is the actual guarantee.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import PipelineError, RetryableError
from .queue import ReceivedMessage

logger = logging.getLogger(__name__)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProcessingOutcome:
    """What a handler decided about a message."""

    handled: bool
    result_reference: str | None = None
    duplicate: bool = False
    error_code: str | None = None
    retryable: bool = False


class InboxGuard:
    """Deduplication ledger keyed on ``(queue_name, message_deduplication_id)``."""

    def __init__(self, database: Any, queue_name: str) -> None:
        self._database = database
        self._queue_name = queue_name

    def already_processed(self, deduplication_id: str) -> bool:
        row = self._database.read_one(
            "SELECT status FROM inbox_messages WHERE queue_name=? AND message_deduplication_id=?",
            (self._queue_name, deduplication_id),
        )
        return bool(row and str(row["status"]) == "processed")

    def record_processed(
        self,
        connection: sqlite3.Connection,
        message: ReceivedMessage,
        *,
        result_reference: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write the inbox row. MUST run inside the business transaction."""
        stamp = _iso(now or datetime.now(UTC))
        connection.execute(
            """
            INSERT INTO inbox_messages(
              queue_name, message_deduplication_id, message_id, status,
              result_reference, receive_count, trace_id, first_seen_at_utc, processed_at_utc
            ) VALUES (?, ?, ?, 'processed', ?, ?, ?, ?, ?)
            ON CONFLICT(queue_name, message_deduplication_id) DO UPDATE SET
              status='processed',
              result_reference=excluded.result_reference,
              receive_count=excluded.receive_count,
              processed_at_utc=excluded.processed_at_utc
            """,
            (
                self._queue_name,
                message.deduplication_id,
                message.message_id,
                result_reference,
                message.receive_count,
                trace_id,
                stamp,
                stamp,
            ),
        )

    def record_failed(
        self,
        message: ReceivedMessage,
        *,
        error_code: str,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record a permanent failure in its own transaction."""
        stamp = _iso(now or datetime.now(UTC))

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO inbox_messages(
                  queue_name, message_deduplication_id, message_id, status,
                  error_code, receive_count, trace_id, first_seen_at_utc, processed_at_utc
                ) VALUES (?, ?, ?, 'failed', ?, ?, ?, ?, ?)
                ON CONFLICT(queue_name, message_deduplication_id) DO UPDATE SET
                  status='failed',
                  error_code=excluded.error_code,
                  receive_count=excluded.receive_count,
                  processed_at_utc=excluded.processed_at_utc
                """,
                (
                    self._queue_name,
                    message.deduplication_id,
                    message.message_id,
                    error_code,
                    message.receive_count,
                    trace_id,
                    stamp,
                    stamp,
                ),
            )

        self._database.write(write)

    def prune(self, *, retention_days: int, now: datetime | None = None) -> int:
        cutoff = _iso((now or datetime.now(UTC)) - timedelta(days=retention_days))

        def delete(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM inbox_messages WHERE queue_name=? AND processed_at_utc IS NOT NULL"
                " AND processed_at_utc < ?",
                (self._queue_name, cutoff),
            )
            return int(cursor.rowcount or 0)

        return self._database.write(delete)


class VisibilityHeartbeat:
    """Extends a message's visibility while long work is in progress.

    Not optional. An expired visibility timeout on a slow segment makes the
    *next* segment of the same station visible early, which breaks the
    per-station ordering that MessageGroupId=station_id was chosen to provide
    (ADR-003).
    """

    def __init__(
        self,
        queue: Any,
        receipt_handle: str,
        *,
        interval_seconds: int,
        visibility_seconds: int,
        max_total_seconds: int,
    ) -> None:
        self._queue = queue
        self._receipt_handle = receipt_handle
        self._interval = max(1, interval_seconds)
        self._visibility = visibility_seconds
        self._deadline = time.monotonic() + max_total_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.extensions = 0

    def __enter__(self) -> VisibilityHeartbeat:
        self._thread = threading.Thread(
            target=self._run, name="visibility-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if time.monotonic() > self._deadline:
                # Beyond the configured processing budget the job is presumed
                # wedged; stop extending and let redelivery take over.
                logger.warning(
                    "Visibility heartbeat budget exhausted; letting the message become visible",
                    extra={"receipt_handle": self._receipt_handle[:24]},
                )
                return
            with contextlib.suppress(Exception):
                self._queue.extend_visibility(self._receipt_handle, seconds=self._visibility)
                self.extensions += 1


class MessageProcessor:
    """Receive loop implementing the six-step consumer contract."""

    def __init__(
        self,
        *,
        database: Any,
        queue: Any,
        queue_name: str,
        component: str,
        visibility_seconds: int = 300,
        heartbeat_seconds: int = 60,
        max_processing_seconds: int = 1800,
        max_messages: int = 5,
        wait_seconds: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._queue = queue
        self._queue_name = queue_name
        self._component = component
        self._visibility_seconds = visibility_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._max_processing_seconds = max_processing_seconds
        self._max_messages = max_messages
        self._wait_seconds = wait_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self.inbox = InboxGuard(database, queue_name)

    def poll_once(
        self, handler: Callable[[ReceivedMessage], ProcessingOutcome]
    ) -> dict[str, int]:
        """Receive a batch and run ``handler`` on each message.

        ``handler`` is responsible for committing the business result together
        with ``inbox.record_processed`` in a single transaction. This loop owns
        the surrounding contract: dedup pre-check, visibility heartbeat, error
        classification, and deleting the message *last*.
        """
        stats = {"received": 0, "processed": 0, "duplicate": 0, "retried": 0, "failed": 0}
        messages = self._queue.receive(
            max_messages=self._max_messages, wait_seconds=self._wait_seconds
        )
        stats["received"] = len(messages)
        for message in messages:
            # Step 3: cheap pre-check. The authoritative check is the UNIQUE
            # constraint inside the handler's transaction; this only avoids
            # redoing expensive work.
            if self.inbox.already_processed(message.deduplication_id):
                self._queue.delete(message.receipt_handle)
                stats["duplicate"] += 1
                continue
            try:
                with VisibilityHeartbeat(
                    self._queue,
                    message.receipt_handle,
                    interval_seconds=self._heartbeat_seconds,
                    visibility_seconds=self._visibility_seconds,
                    max_total_seconds=self._max_processing_seconds,
                ):
                    outcome = handler(message)
            except PipelineError as error:
                if error.retryable:
                    # Leave the message. Visibility expiry redelivers it.
                    self._record_failure(message, error)
                    stats["retried"] += 1
                    continue
                self._record_failure(message, error)
                self.inbox.record_failed(
                    message, error_code=error.code, now=self._clock()
                )
                self._queue.delete(message.receipt_handle)
                stats["failed"] += 1
                continue
            except Exception as error:  # noqa: BLE001 - unknown failures are retryable
                wrapped = RetryableError(
                    f"Unhandled {type(error).__name__} in {self._component}",
                    detail=str(error)[:800],
                )
                self._record_failure(message, wrapped)
                stats["retried"] += 1
                continue

            if outcome.duplicate:
                self._queue.delete(message.receipt_handle)
                stats["duplicate"] += 1
            elif outcome.handled:
                # Step 6: the business result is already durable.
                self._queue.delete(message.receipt_handle)
                stats["processed"] += 1
            elif outcome.retryable:
                stats["retried"] += 1
            else:
                self.inbox.record_failed(
                    message,
                    error_code=outcome.error_code or "handler_rejected",
                    now=self._clock(),
                )
                self._queue.delete(message.receipt_handle)
                stats["failed"] += 1
        return stats

    def _record_failure(self, message: ReceivedMessage, error: PipelineError) -> None:
        record = error.as_failure_record()
        stamp = _iso(self._clock())

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO processing_failures(
                  component, error_code, retryable, queue_name,
                  message_deduplication_id, message, detail, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._component,
                    record["code"],
                    1 if record["retryable"] else 0,
                    self._queue_name,
                    message.deduplication_id,
                    record["message"],
                    record["detail"],
                    stamp,
                ),
            )

        with contextlib.suppress(Exception):
            # Never let failure bookkeeping mask the original failure.
            self._database.write(write)
        logger.warning(
            "Message processing failed",
            extra={
                "component": self._component,
                "queue": self._queue_name,
                "error_code": record["code"],
                "retryable": record["retryable"],
                "dedup_id": message.deduplication_id,
            },
        )


__all__ = [
    "InboxGuard",
    "MessageProcessor",
    "ProcessingOutcome",
    "VisibilityHeartbeat",
]
