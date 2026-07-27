"""Transactional outbox (ADR-009).

SQLite and SQS cannot share a transaction. Writing business state and then
sending a message has two failure windows: a crash after the commit leaves work
that is never queued (a *silent stall* — nothing errors, the work simply never
happens), and a crash after the send leaves a resend on restart.

The outbox closes the first window by making "record the intent to send" part of
the same transaction as the business write. The consumer inbox
(:mod:`app.pipeline.idempotency`) closes the second by making a resend a no-op.

Producers never call SQS. They call :func:`enqueue` inside their own
transaction; a dispatcher in the planner process does the sending.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import PipelineError
from .ids import new_id

logger = logging.getLogger(__name__)

#: Backoff schedule for a failed send, in seconds, indexed by attempt count.
#: Capped so a long SQS outage does not push retries hours into the future.
_BACKOFF_SECONDS = (2, 5, 15, 30, 60, 120, 300, 600, 900, 1800)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def backoff_delay(attempts: int, *, jitter: Callable[[], float] | None = None) -> float:
    """Delay before the next send attempt, with full jitter.

    Jitter is not decoration: without it, every event that failed during the
    same outage retries at the same instant and reproduces the thundering herd
    that caused the failure.
    """
    index = min(max(attempts, 0), len(_BACKOFF_SECONDS) - 1)
    base = _BACKOFF_SECONDS[index]
    return base * (jitter or random.random)()


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    queue_name: str
    message_group_id: str
    message_deduplication_id: str
    payload_json: str
    attempts: int
    trace_id: str | None


def enqueue(
    connection: sqlite3.Connection,
    *,
    queue_name: str,
    message_group_id: str,
    message_deduplication_id: str,
    payload: str,
    trace_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Record an intent to send, inside the caller's transaction.

    Must be called from within an open transaction that also writes the
    business state — that co-location is the entire guarantee.

    Idempotent on ``(queue_name, message_deduplication_id)``: re-enqueuing the
    same logical message is a no-op rather than a duplicate, which makes the
    caller safe to retry.
    """
    stamp = _iso(now or datetime.now(UTC))
    event_id = new_id()
    connection.execute(
        """
        INSERT INTO outbox_events(
          event_id, queue_name, message_group_id, message_deduplication_id,
          payload_json, status, attempts, available_at_utc, trace_id,
          created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
        ON CONFLICT(queue_name, message_deduplication_id) DO NOTHING
        """,
        (
            event_id,
            queue_name,
            message_group_id,
            message_deduplication_id,
            payload,
            stamp,
            trace_id,
            stamp,
            stamp,
        ),
    )
    return event_id


class OutboxDispatcher:
    """Claims pending events, sends them, records the outcome.

    Ordering is deliberate and load-bearing:

    1. claim a batch in one short transaction (lock held briefly);
    2. **release the transaction**;
    3. call SQS — network I/O never runs inside a transaction (ADR-004 §3);
    4. record the result in a second short transaction.
    """

    def __init__(
        self,
        database: Any,
        queues: dict[str, Any],
        *,
        batch_size: int = 25,
        max_attempts: int = 10,
        lease_seconds: int = 120,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._database = database
        self._queues = queues
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jitter = jitter or random.random

    def dispatch_once(self) -> dict[str, int]:
        """One dispatch cycle. Returns counters for logging and metrics."""
        stats = {"claimed": 0, "sent": 0, "retried": 0, "failed": 0, "skipped": 0}
        self.reclaim_stale_leases()
        events = self._claim_batch()
        stats["claimed"] = len(events)
        for event in events:
            queue = self._queues.get(event.queue_name)
            if queue is None:
                # A configuration problem, not a message problem. Return it to
                # pending so it drains once the queue is wired, rather than
                # burning an attempt each cycle.
                self._release(event, error=f"No queue configured for {event.queue_name!r}")
                stats["skipped"] += 1
                continue
            try:
                message_id = queue.send(
                    event.payload_json,
                    group_id=event.message_group_id,
                    deduplication_id=event.message_deduplication_id,
                )
            except Exception as error:  # noqa: BLE001 - classified below
                detail = _error_detail(error)
                if event.attempts + 1 >= self._max_attempts:
                    self._mark_failed(event, detail)
                    stats["failed"] += 1
                else:
                    self._schedule_retry(event, detail)
                    stats["retried"] += 1
                continue
            self._mark_sent(event, message_id)
            stats["sent"] += 1
        return stats

    # -- claim / record -------------------------------------------------------

    def _claim_batch(self) -> list[OutboxEvent]:
        now = self._clock()
        lease_until = _iso(now + timedelta(seconds=self._lease_seconds))

        def claim(connection: sqlite3.Connection) -> list[OutboxEvent]:
            rows = connection.execute(
                """
                SELECT event_id, queue_name, message_group_id, message_deduplication_id,
                       payload_json, attempts, trace_id
                FROM outbox_events
                WHERE status = 'pending' AND available_at_utc <= ?
                ORDER BY available_at_utc, created_at_utc
                LIMIT ?
                """,
                (_iso(now), self._batch_size),
            ).fetchall()
            claimed: list[OutboxEvent] = []
            for row in rows:
                connection.execute(
                    "UPDATE outbox_events SET status='sending', lease_expires_at_utc=?,"
                    " updated_at_utc=? WHERE event_id=? AND status='pending'",
                    (lease_until, _iso(now), str(row["event_id"])),
                )
                claimed.append(
                    OutboxEvent(
                        event_id=str(row["event_id"]),
                        queue_name=str(row["queue_name"]),
                        message_group_id=str(row["message_group_id"]),
                        message_deduplication_id=str(row["message_deduplication_id"]),
                        payload_json=str(row["payload_json"]),
                        attempts=int(row["attempts"]),
                        trace_id=row["trace_id"],
                    )
                )
            return claimed

        return self._database.write(claim)

    def _mark_sent(self, event: OutboxEvent, message_id: str) -> None:
        stamp = _iso(self._clock())
        self._database.write(
            lambda connection: connection.execute(
                "UPDATE outbox_events SET status='sent', sqs_message_id=?, sent_at_utc=?,"
                " lease_expires_at_utc=NULL, last_error=NULL, updated_at_utc=?"
                " WHERE event_id=?",
                (message_id, stamp, stamp, event.event_id),
            )
        )

    def _schedule_retry(self, event: OutboxEvent, detail: str) -> None:
        now = self._clock()
        delay = backoff_delay(event.attempts, jitter=self._jitter)
        available = _iso(now + timedelta(seconds=delay))
        self._database.write(
            lambda connection: connection.execute(
                "UPDATE outbox_events SET status='pending', attempts=attempts+1,"
                " available_at_utc=?, lease_expires_at_utc=NULL, last_error=?, updated_at_utc=?"
                " WHERE event_id=?",
                (available, detail[:500], _iso(now), event.event_id),
            )
        )

    def _release(self, event: OutboxEvent, *, error: str) -> None:
        """Return an event to pending without consuming an attempt."""
        now = self._clock()
        available = _iso(now + timedelta(seconds=backoff_delay(0, jitter=self._jitter)))
        self._database.write(
            lambda connection: connection.execute(
                "UPDATE outbox_events SET status='pending', available_at_utc=?,"
                " lease_expires_at_utc=NULL, last_error=?, updated_at_utc=? WHERE event_id=?",
                (available, error[:500], _iso(now), event.event_id),
            )
        )

    def _mark_failed(self, event: OutboxEvent, detail: str) -> None:
        stamp = _iso(self._clock())
        payload_preview = event.payload_json[:400]

        def record(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE outbox_events SET status='failed', attempts=attempts+1,"
                " lease_expires_at_utc=NULL, last_error=?, updated_at_utc=? WHERE event_id=?",
                (detail[:500], stamp, event.event_id),
            )
            connection.execute(
                """
                INSERT INTO processing_failures(
                  component, error_code, retryable, queue_name,
                  message_deduplication_id, message, detail, trace_id, created_at_utc
                ) VALUES ('outbox', 'outbox_exhausted', 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.queue_name,
                    event.message_deduplication_id,
                    f"Outbox event abandoned after {self._max_attempts} attempts",
                    f"{detail[:800]} payload_preview={payload_preview}",
                    event.trace_id,
                    stamp,
                ),
            )

        self._database.write(record)
        # Failed rows are never auto-pruned: they are the evidence that work was
        # lost, and an operator needs to see them.
        logger.error(
            "Outbox event abandoned",
            extra={
                "event_id": event.event_id,
                "queue": event.queue_name,
                "dedup_id": event.message_deduplication_id,
            },
        )

    def reclaim_stale_leases(self) -> int:
        """Return events whose dispatcher died mid-send back to pending.

        This may resend a message that SQS actually accepted. That is exactly
        the case the consumer inbox exists to absorb.
        """
        now = self._clock()
        cutoff = _iso(now)

        def reclaim(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "UPDATE outbox_events SET status='pending', lease_expires_at_utc=NULL,"
                " updated_at_utc=? WHERE status='sending' AND lease_expires_at_utc IS NOT NULL"
                " AND lease_expires_at_utc <= ?",
                (cutoff, cutoff),
            )
            return int(cursor.rowcount or 0)

        reclaimed = self._database.write(reclaim)
        if reclaimed:
            logger.warning("Reclaimed %d stale outbox leases", reclaimed)
        return reclaimed

    # -- metrics --------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        rows = self._database.read_all(
            "SELECT status, count(*) AS n FROM outbox_events GROUP BY status"
        )
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        oldest = self._database.read_one(
            "SELECT min(created_at_utc) AS oldest FROM outbox_events WHERE status='pending'"
        )
        oldest_at = _parse(oldest["oldest"] if oldest else None)
        age = (
            max(0.0, (self._clock() - oldest_at).total_seconds())
            if oldest_at is not None
            else None
        )
        return {
            "pending": counts.get("pending", 0),
            "sending": counts.get("sending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "oldest_pending_seconds": round(age, 1) if age is not None else None,
        }

    def prune(self, *, retention_days: int) -> int:
        """Delete old ``sent`` rows. ``failed`` rows are never pruned."""
        cutoff = _iso(self._clock() - timedelta(days=retention_days))

        def delete(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM outbox_events WHERE status='sent' AND updated_at_utc < ?",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

        return self._database.write(delete)


def _error_detail(error: Exception) -> str:
    if isinstance(error, PipelineError):
        return f"{error.code}: {error.message}"
    return f"{type(error).__name__}: {error}"


def payload_of(model: Any) -> str:
    """Serialise a contract model (or a plain mapping) for the outbox."""
    if hasattr(model, "to_body"):
        return model.to_body()
    return json.dumps(model, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "OutboxDispatcher",
    "OutboxEvent",
    "backoff_delay",
    "enqueue",
    "payload_of",
]
