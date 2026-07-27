"""Amazon SQS FIFO queue backend (ADR-003).

Thin by design: every reliability decision lives in the worker loop (inbox,
outbox, error classification), so this module only translates the queue
protocol to boto3 and classifies transport failures.

Notably absent: any code that sends to a dead-letter queue. Redrive is a queue
attribute (`RedrivePolicy`/`maxReceiveCount`) owned by infrastructure. A
permanent failure is recorded in `processing_failures` and the message is then
deleted, so a message whose failure we already understand does not burn
`maxReceiveCount` attempts.
"""
from __future__ import annotations

import logging
from typing import Any

from .errors import QueueUnavailableError
from .queue import ReceivedMessage

logger = logging.getLogger(__name__)

#: SQS returns at most 10 messages per ReceiveMessage call.
MAX_RECEIVE_BATCH = 10
#: Documented long-polling maximum.
MAX_WAIT_SECONDS = 20
#: Documented visibility-timeout maximum (12 hours).
MAX_VISIBILITY_SECONDS = 43_200


class SqsQueue:
    """FIFO queue client."""

    def __init__(
        self,
        sqs_client: Any,
        queue_url: str,
        *,
        visibility_seconds: int = 300,
    ) -> None:
        self._sqs = sqs_client
        self._queue_url = queue_url.strip()
        if not self._queue_url:
            raise ValueError("SqsQueue requires a queue URL")
        if not self._queue_url.rstrip("/").endswith(".fifo"):
            # Ordering per station is a correctness requirement, not a
            # preference: a standard queue would silently reorder segments.
            raise ValueError(
                f"SqsQueue requires a FIFO queue URL (ending in .fifo), got {self._queue_url!r}"
            )
        self._visibility_seconds = max(30, min(visibility_seconds, MAX_VISIBILITY_SECONDS))

    @property
    def name(self) -> str:
        return self._queue_url.rsplit("/", 1)[-1]

    @property
    def queue_url(self) -> str:
        return self._queue_url

    def send(self, body: str, *, group_id: str, deduplication_id: str) -> str:
        try:
            response = self._sqs.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                MessageGroupId=group_id,
                MessageDeduplicationId=deduplication_id,
            )
        except Exception as error:  # noqa: BLE001 - transport failures are retryable
            raise QueueUnavailableError(
                "SQS send_message failed", detail=str(error)[:400]
            ) from error
        return str(response.get("MessageId") or "")

    def receive(self, *, max_messages: int = 1, wait_seconds: int = 0) -> list[ReceivedMessage]:
        try:
            response = self._sqs.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=max(1, min(max_messages, MAX_RECEIVE_BATCH)),
                WaitTimeSeconds=max(0, min(wait_seconds, MAX_WAIT_SECONDS)),
                VisibilityTimeout=self._visibility_seconds,
                AttributeNames=["ApproximateReceiveCount", "MessageGroupId", "MessageDeduplicationId"],
            )
        except Exception as error:  # noqa: BLE001 - transport failures are retryable
            raise QueueUnavailableError(
                "SQS receive_message failed", detail=str(error)[:400]
            ) from error

        output: list[ReceivedMessage] = []
        for item in response.get("Messages", []) or []:
            attributes = dict(item.get("Attributes") or {})
            try:
                receive_count = int(attributes.get("ApproximateReceiveCount", "1"))
            except (TypeError, ValueError):
                receive_count = 1
            output.append(
                ReceivedMessage(
                    message_id=str(item.get("MessageId") or ""),
                    receipt_handle=str(item.get("ReceiptHandle") or ""),
                    body=str(item.get("Body") or ""),
                    group_id=str(attributes.get("MessageGroupId") or ""),
                    deduplication_id=str(attributes.get("MessageDeduplicationId") or ""),
                    receive_count=receive_count,
                    attributes=attributes,
                )
            )
        return output

    def delete(self, receipt_handle: str) -> None:
        try:
            self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt_handle)
        except Exception as error:  # noqa: BLE001 - transport failures are retryable
            raise QueueUnavailableError(
                "SQS delete_message failed", detail=str(error)[:400]
            ) from error

    def extend_visibility(self, receipt_handle: str, *, seconds: int) -> None:
        bounded = max(0, min(seconds, MAX_VISIBILITY_SECONDS))
        try:
            self._sqs.change_message_visibility(
                QueueUrl=self._queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=bounded,
            )
        except Exception as error:  # noqa: BLE001
            # Do not fail the job for a failed extension: the work may still
            # finish before the original timeout, and duplicate delivery is
            # already handled by the inbox.
            logger.warning(
                "Visibility extension failed",
                extra={"queue": self.name, "error": str(error)[:200]},
            )

    def approximate_depth(self) -> int | None:
        try:
            response = self._sqs.get_queue_attributes(
                QueueUrl=self._queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
        except Exception:  # noqa: BLE001 - diagnostics only, never fail health on it
            return None
        try:
            return int(response["Attributes"]["ApproximateNumberOfMessages"])
        except (KeyError, TypeError, ValueError):
            return None


__all__ = ["MAX_RECEIVE_BATCH", "MAX_VISIBILITY_SECONDS", "MAX_WAIT_SECONDS", "SqsQueue"]
