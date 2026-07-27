"""Shared-station SQS pipeline primitives.

Import-safe in every pipeline mode: nothing here constructs an AWS client,
opens a socket, touches the filesystem or loads a model at import time. That
property is what lets `RADIO_PIPELINE_MODE=legacy` remain byte-identical to the
pre-pipeline behaviour (ADR-001).
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
