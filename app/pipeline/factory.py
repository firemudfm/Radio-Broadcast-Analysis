"""Construction of queues and segment stores from configuration.

Centralised so that every worker resolves the same backend from the same
settings. A listener writing to the local spool while a transcription worker
reads from S3 is a silent, total failure -- segments are produced, nothing is
consumed, and every health check stays green -- so backend selection happens in
exactly one place.

The in-memory queue registry is process-scoped and keyed by queue name, so a
producer and a consumer inside one process (integration tests, and the
single-process ``RADIO_QUEUE_BACKEND=memory`` mode) share the same instance
rather than silently talking past each other.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import Settings
from .local_segment_store import LocalSegmentStore
from .queue import MemoryQueue, MessageQueue
from .s3_segment_store import S3SegmentStore
from .segment_store import SegmentStore
from .sqs_queue import SqsQueue

logger = logging.getLogger(__name__)

TRANSCRIPTION_QUEUE = "transcription"
ANALYSIS_QUEUE = "analysis"

_MEMORY_QUEUES: dict[str, MemoryQueue] = {}
_LOCK = threading.Lock()


def reset_memory_queues() -> None:
    """Drop every in-process queue. Test isolation only."""
    with _LOCK:
        _MEMORY_QUEUES.clear()


def build_queue(settings: Settings, name: str) -> MessageQueue:
    """Return the queue for ``name`` under the configured backend."""
    if settings.RADIO_QUEUE_BACKEND == "memory":
        with _LOCK:
            queue = _MEMORY_QUEUES.get(name)
            if queue is None:
                queue = MemoryQueue(
                    f"{name}.fifo",
                    visibility_seconds=settings.RADIO_SQS_VISIBILITY_SECONDS,
                )
                _MEMORY_QUEUES[name] = queue
            return queue

    url = _queue_url(settings, name)
    return SqsQueue(
        _sqs_client(settings),
        url,
        visibility_seconds=settings.RADIO_SQS_VISIBILITY_SECONDS,
    )


def build_queues(settings: Settings) -> dict[str, MessageQueue]:
    """Both queues, keyed by the names the outbox dispatcher uses."""
    return {
        TRANSCRIPTION_QUEUE: build_queue(settings, TRANSCRIPTION_QUEUE),
        ANALYSIS_QUEUE: build_queue(settings, ANALYSIS_QUEUE),
    }


def _queue_url(settings: Settings, name: str) -> str:
    if name == TRANSCRIPTION_QUEUE:
        return settings.RADIO_TRANSCRIPTION_QUEUE_URL
    if name == ANALYSIS_QUEUE:
        return settings.RADIO_ANALYSIS_QUEUE_URL
    raise ValueError(f"Unknown queue name: {name!r}")


def _sqs_client(settings: Settings) -> Any:
    import boto3  # noqa: PLC0415 - only needed for the SQS backend

    return boto3.client("sqs", region_name=settings.effective_aws_region)


def build_s3_client(settings: Settings) -> Any:
    import boto3  # noqa: PLC0415 - only needed when S3 is actually used

    return boto3.client("s3", region_name=settings.effective_aws_region)


def build_segment_store(
    settings: Settings, *, s3_client: Any | None = None
) -> SegmentStore:
    """Return the configured segment store.

    ``local`` is the production default for the single-node deployment: copying
    every 20-second segment to S3 would add a network round trip and a per-object
    cost to audio that is usually deleted minutes later (ADR-002).
    """
    if settings.RADIO_SEGMENT_STORE == "s3":
        store = S3SegmentStore(
            s3_client or build_s3_client(settings),
            settings.RADIO_S3_BUCKET,
            prefix=settings.RADIO_TEMP_SPEECH_PREFIX,
        )
        logger.info("Segment store: s3", extra={"bucket": settings.RADIO_S3_BUCKET})
        return store

    store = LocalSegmentStore(settings.RADIO_SPOOL_PATH)
    store.ensure_root()
    return store


__all__ = [
    "ANALYSIS_QUEUE",
    "TRANSCRIPTION_QUEUE",
    "build_queue",
    "build_queues",
    "build_s3_client",
    "build_segment_store",
    "reset_memory_queues",
]
