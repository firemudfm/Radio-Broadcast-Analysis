"""Error taxonomy for pipeline workers.

The single question every consumer must answer about a failure is *should this
message be redelivered?* Getting that wrong in one direction loses work
silently; in the other it produces a poison message that retries forever. So
retryability is a property of the exception type, not a decision made at each
call site.

Handling contract (ADR-009 §3):

* ``retryable`` -> leave the message; let the visibility timeout expire.
* not ``retryable`` -> record in ``processing_failures``, write the inbox row,
  then delete the message.

The application never sends a message to a dead-letter queue itself; redrive is
a queue attribute. Deleting a message whose failure we already understand stops
it consuming ``maxReceiveCount`` attempts for a known reason.
"""
from __future__ import annotations


class PipelineError(Exception):
    """Base class for every pipeline failure.

    Attributes
    ----------
    retryable:
        Whether redelivery could plausibly succeed.
    code:
        Stable machine-readable identifier recorded in ``processing_failures``
        and used for metrics. Never a free-form message.
    """

    retryable: bool = False
    code: str = "pipeline_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_failure_record(self) -> dict[str, object]:
        """Bounded, safe-to-store representation.

        Truncated because failure detail is attacker-influenced in the general
        case (it can contain remote stream metadata) and because an unbounded
        string in SQLite is a slow-growth disk problem.
        """
        return {
            "code": self.code,
            "retryable": self.retryable,
            "message": self.message[:500],
            "detail": (self.detail or "")[:2000] or None,
        }


# --- retryable ---------------------------------------------------------------


class RetryableError(PipelineError):
    retryable = True
    code = "retryable"


class DatabaseUnavailableError(RetryableError):
    """SQLite is locked or the file is temporarily unreachable."""

    code = "database_unavailable"


class ResourceExhaustedError(RetryableError):
    """Disk, memory or a worker slot is exhausted; back off harder."""

    code = "resource_exhausted"


class QueueUnavailableError(RetryableError):
    """SQS itself failed. Not the message's fault."""

    code = "queue_unavailable"


class UpstreamUnavailableError(RetryableError):
    """A dependency (LLM, S3, stream) is temporarily unavailable."""

    code = "upstream_unavailable"


class TranscriptionFailedError(RetryableError):
    """The ASR engine failed in a way that a retry might survive."""

    code = "transcription_failed"


class AnalysisFailedError(RetryableError):
    """The LLM failed in a way that a retry might survive."""

    code = "analysis_failed"


# --- permanent ---------------------------------------------------------------


class PermanentError(PipelineError):
    retryable = False
    code = "permanent"


class InvalidMessageError(PermanentError):
    """Schema violation, unknown version, oversized field, malformed id.

    A malformed message will never become well-formed, so redelivery is pure
    waste.
    """

    code = "invalid_message"


class UnsupportedSchemaError(InvalidMessageError):
    code = "unsupported_schema"


class MessageTooLargeError(InvalidMessageError):
    code = "message_too_large"


class SegmentMissingError(PermanentError):
    """The referenced audio no longer exists. It will not come back."""

    code = "segment_missing"


class ChecksumMismatchError(PermanentError):
    """Stored bytes do not match the recorded SHA-256.

    Treated as permanent and quarantined: silently transcribing bytes that are
    not what the producer wrote would launder corruption (or tampering) into
    the evidence record.
    """

    code = "checksum_mismatch"


class SegmentAccessError(PermanentError):
    """Path traversal, symlink escape, or a backend/configuration mismatch."""

    code = "segment_access_denied"


class UnsupportedModelError(PermanentError):
    """The message names a model this consumer version cannot serve."""

    code = "unsupported_model"


class ClassifierUnavailableError(PermanentError):
    """A requested classifier backend is not deployable on this platform.

    Raised by the reserved ``yamnet`` backend. It fails loudly rather than
    silently degrading to a different model (ADR-005).
    """

    code = "classifier_unavailable"


class ModelVerificationError(PermanentError):
    """A model file is missing, truncated, or fails its pinned digest."""

    code = "model_verification_failed"


class CircuitOpenError(RetryableError):
    """The circuit breaker is open; the dependency is being given time."""

    code = "circuit_open"


__all__ = [
    "AnalysisFailedError",
    "ChecksumMismatchError",
    "CircuitOpenError",
    "ClassifierUnavailableError",
    "DatabaseUnavailableError",
    "InvalidMessageError",
    "MessageTooLargeError",
    "ModelVerificationError",
    "PermanentError",
    "PipelineError",
    "QueueUnavailableError",
    "ResourceExhaustedError",
    "RetryableError",
    "SegmentAccessError",
    "SegmentMissingError",
    "TranscriptionFailedError",
    "UnsupportedModelError",
    "UnsupportedSchemaError",
    "UpstreamUnavailableError",
]
