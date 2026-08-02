"""Shared-station SQS pipeline primitives.

Import-safe by design: nothing here constructs an AWS client, opens a socket,
touches the filesystem or loads a model at import time. That keeps the API
process cheap to start and makes these primitives usable from a test without
any of the infrastructure they describe.
"""
from __future__ import annotations

__all__ = [
    "contracts",
    "enums",
    "errors",
    "idempotency",
    "ids",
    "local_segment_store",
    "outbox",
    "queue",
    "s3_segment_store",
    "segment_store",
    "sqs_queue",
]
