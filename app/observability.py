"""Structured logging and trace propagation.

Two rules:

1. **Ids, not bodies.** At INFO level a log line carries ``trace_id``,
   ``station_id``, ``segment_id``, ``conversation_id``, ``mention_id`` and
   ``job_id`` — never a transcript, never a message body, never a stream URL.
   Broadcast content is the product; it does not belong in operational logs, and
   `RADIO_LOG_TRANSCRIPT_BODIES` must be set explicitly to change that.
2. **One JSON object per line** when ``RADIO_LOG_FORMAT=json``, so the output is
   machine-readable without a parser that has to guess at multi-line records.

The trace id lives in a :class:`contextvars.ContextVar`, so it survives
``asyncio.to_thread`` and async task boundaries without being threaded through
every function signature.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: Fields lifted from `extra=` onto the top level of a JSON log record.
TRACE_FIELDS: tuple[str, ...] = (
    "trace_id",
    "station_id",
    "segment_id",
    "conversation_id",
    "mention_id",
    "job_id",
    "campaign_id",
    "keyword_id",
    "worker_id",
    "shard_index",
)

#: Values that must never be logged, even if a caller passes them.
_REDACTED_KEYS = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "authorization",
        "password",
        "secret",
        "token",
        "presigned_url",
        "stream_url",
        "url_resolved",
        "radio_audio_token_secret",
    }
)

_REDACTED = "[redacted]"

_trace_id: ContextVar[str | None] = ContextVar("radio_trace_id", default=None)


def current_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(value: str | None) -> None:
    _trace_id.set(value)


@contextmanager
def trace_context(trace_id: str | None):
    """Bind ``trace_id`` for the duration of the block."""
    token = _trace_id.set(trace_id)
    try:
        yield trace_id
    finally:
        _trace_id.reset(token)


class TraceFilter(logging.Filter):
    """Injects the ambient trace id into every record that lacks one."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = current_trace_id()  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in TRACE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        for key, value in record.__dict__.items():
            if key in payload or key in _LOG_RECORD_BUILTINS or key in TRACE_FIELDS:
                continue
            payload[key] = _REDACTED if key.lower() in _REDACTED_KEYS else _safe(value)
        if record.exc_info:
            # The type and message only. A full traceback can carry filesystem
            # paths and argument values into an aggregator.
            exc_type, exc_value, _ = record.exc_info
            payload["error_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["error"] = str(exc_value)[:500]
        return json.dumps(payload, ensure_ascii=False, default=str)


_LOG_RECORD_BUILTINS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName",
    }
)


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:1000]
    return str(value)[:1000]


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "text",
    stream: Any | None = None,
) -> None:
    """Install the root handler. Safe to call more than once."""
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(TraceFilter())
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(trace_id)s] %(message)s")
        )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)


def log_fields(**fields: Any) -> dict[str, Any]:
    """Build an ``extra=`` mapping, dropping Nones and redacting secrets."""
    output: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        output[key] = _REDACTED if key.lower() in _REDACTED_KEYS else _safe(value)
    return output


__all__ = [
    "JsonFormatter",
    "TRACE_FIELDS",
    "TraceFilter",
    "configure_logging",
    "current_trace_id",
    "log_fields",
    "set_trace_id",
    "trace_context",
]
