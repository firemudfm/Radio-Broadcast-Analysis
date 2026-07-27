"""Queue abstraction plus a FIFO-faithful in-process backend.

``MemoryQueue`` is not a stub. It reproduces the SQS FIFO semantics the design
depends on, because those semantics are the thing most likely to be got wrong:

* per-``MessageGroupId`` ordering;
* **at most one in-flight message per group** — no further message from a group
  is returned until the in-flight one is deleted or its visibility expires
  (this is the constraint that shapes ADR-003 and the capacity default);
* a 5-minute deduplication window keyed on ``MessageDeduplicationId``;
* visibility timeouts, extension, and redelivery on expiry;
* a receive counter, so ``maxReceiveCount``-style behaviour is observable.

The contract tests run against both backends, so a divergence between the
in-memory model and real SQS shows up as a test failure rather than in
production.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: SQS FIFO deduplication interval, from the SQS Developer Guide.
DEDUPLICATION_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class ReceivedMessage:
    """One message plus the handle needed to delete or extend it."""

    message_id: str
    receipt_handle: str
    body: str
    group_id: str
    deduplication_id: str
    receive_count: int = 1
    attributes: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class MessageQueue(Protocol):
    """Minimal queue surface the workers rely on."""

    def send(
        self,
        body: str,
        *,
        group_id: str,
        deduplication_id: str,
    ) -> str:
        """Enqueue and return the provider message id."""

    def receive(self, *, max_messages: int = 1, wait_seconds: int = 0) -> list[ReceivedMessage]:
        """Return up to ``max_messages``, respecting per-group exclusivity."""

    def delete(self, receipt_handle: str) -> None:
        """Acknowledge. Called only after the business result is durable."""

    def extend_visibility(self, receipt_handle: str, *, seconds: int) -> None:
        """Keep a long-running message invisible while work continues."""


@dataclass
class _Entry:
    message_id: str
    body: str
    group_id: str
    deduplication_id: str
    enqueued_at: float
    sequence: int
    receive_count: int = 0
    visible_at: float = 0.0
    receipt_handle: str | None = None


class MemoryQueue:
    """In-process FIFO queue with SQS-equivalent group semantics."""

    def __init__(
        self,
        name: str = "memory.fifo",
        *,
        visibility_seconds: int = 300,
        deduplication_window_seconds: int = DEDUPLICATION_WINDOW_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.name = name
        self._visibility_seconds = visibility_seconds
        self._dedup_window = deduplication_window_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: list[_Entry] = []
        self._in_flight: dict[str, _Entry] = {}
        self._dedup: OrderedDict[str, float] = OrderedDict()
        self._sequence = 0
        self._message_counter = 0

    # -- producer -------------------------------------------------------------

    def send(self, body: str, *, group_id: str, deduplication_id: str) -> str:
        with self._lock:
            now = self._clock()
            self._expire_dedup(now)
            if deduplication_id in self._dedup:
                # SQS silently accepts and drops a duplicate inside the window.
                return f"dedup-{deduplication_id}"
            self._dedup[deduplication_id] = now
            self._sequence += 1
            self._message_counter += 1
            message_id = f"{self.name}-{self._message_counter}"
            self._entries.append(
                _Entry(
                    message_id=message_id,
                    body=body,
                    group_id=group_id,
                    deduplication_id=deduplication_id,
                    enqueued_at=now,
                    sequence=self._sequence,
                )
            )
            return message_id

    # -- consumer -------------------------------------------------------------

    def receive(self, *, max_messages: int = 1, wait_seconds: int = 0) -> list[ReceivedMessage]:
        del wait_seconds  # In-process: nothing to wait for.
        with self._lock:
            now = self._clock()
            self._reclaim_expired(now)
            blocked_groups = {entry.group_id for entry in self._in_flight.values()}
            selected: list[ReceivedMessage] = []
            for entry in sorted(self._entries, key=lambda item: item.sequence):
                if len(selected) >= max_messages:
                    break
                if entry.group_id in blocked_groups:
                    # The defining FIFO rule: one in-flight message per group.
                    continue
                blocked_groups.add(entry.group_id)
                entry.receive_count += 1
                entry.visible_at = now + self._visibility_seconds
                entry.receipt_handle = f"{entry.message_id}#{entry.receive_count}"
                self._in_flight[entry.receipt_handle] = entry
                selected.append(
                    ReceivedMessage(
                        message_id=entry.message_id,
                        receipt_handle=entry.receipt_handle,
                        body=entry.body,
                        group_id=entry.group_id,
                        deduplication_id=entry.deduplication_id,
                        receive_count=entry.receive_count,
                    )
                )
            for message in selected:
                entry = self._in_flight[message.receipt_handle]
                self._entries.remove(entry)
            return selected

    def delete(self, receipt_handle: str) -> None:
        with self._lock:
            self._in_flight.pop(receipt_handle, None)

    def extend_visibility(self, receipt_handle: str, *, seconds: int) -> None:
        with self._lock:
            entry = self._in_flight.get(receipt_handle)
            if entry is not None:
                entry.visible_at = self._clock() + seconds

    # -- introspection (tests and diagnostics) --------------------------------

    def approximate_depth(self) -> int:
        with self._lock:
            self._reclaim_expired(self._clock())
            return len(self._entries)

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def oldest_message_age_seconds(self) -> float | None:
        with self._lock:
            if not self._entries:
                return None
            now = self._clock()
            return round(now - min(entry.enqueued_at for entry in self._entries), 3)

    # -- internals ------------------------------------------------------------

    def _reclaim_expired(self, now: float) -> None:
        """Return messages whose visibility lapsed, preserving group order."""
        expired = [
            handle for handle, entry in self._in_flight.items() if entry.visible_at <= now
        ]
        for handle in expired:
            entry = self._in_flight.pop(handle)
            entry.receipt_handle = None
            self._entries.append(entry)

    def _expire_dedup(self, now: float) -> None:
        cutoff = now - self._dedup_window
        while self._dedup:
            key, stamp = next(iter(self._dedup.items()))
            if stamp > cutoff:
                break
            self._dedup.popitem(last=False)


__all__ = [
    "DEDUPLICATION_WINDOW_SECONDS",
    "MemoryQueue",
    "MessageQueue",
    "ReceivedMessage",
]
