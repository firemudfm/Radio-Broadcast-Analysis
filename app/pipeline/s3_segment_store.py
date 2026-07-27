"""S3 segment store — distributed mode, recovery and overflow (ADR-002).

Not the single-node default. Used when workers run on a different host from the
listener, for cross-host handoff, explicit overflow, and troubleshooting.

Every write is server-side encrypted, carries the SHA-256 as object metadata and
as an S3 checksum, and uses a deterministic key so a retry overwrites the same
object rather than creating a second one. Presigned URLs are never generated —
the object key travels in the message and the reader authenticates with its own
role.
"""
from __future__ import annotations

import logging
from typing import Any

from .contracts import StorageDescriptor
from .errors import SegmentAccessError, SegmentMissingError, UpstreamUnavailableError
from .segment_store import SegmentRef, sha256_hex, verify_digest

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


class S3SegmentStore:
    """Segment store backed by a single S3 prefix."""

    backend = "s3"

    def __init__(self, s3_client: Any, bucket: str, prefix: str = "temp-speech/") -> None:
        self._s3 = s3_client
        self._bucket = bucket.strip()
        if not self._bucket:
            raise ValueError("S3SegmentStore requires a bucket name")
        cleaned = prefix.strip().lstrip("/")
        self._prefix = cleaned if cleaned.endswith("/") else f"{cleaned}/"

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def key_for(self, ref: SegmentRef) -> str:
        """Deterministic key. Retries overwrite; they never duplicate."""
        return f"{self._prefix}{ref.relative_path}"

    def _validated_key(self, descriptor: StorageDescriptor) -> str:
        if descriptor.backend != "s3":
            raise SegmentAccessError(
                f"S3SegmentStore cannot read a {descriptor.backend!r} descriptor; "
                f"the producer and consumer disagree about RADIO_SEGMENT_STORE"
            )
        if (descriptor.bucket or "") != self._bucket:
            raise SegmentAccessError(
                "Refusing a segment descriptor for a different bucket",
                detail=f"descriptor_bucket={descriptor.bucket!r}",
            )
        key = str(descriptor.key or "")
        if not key.startswith(self._prefix):
            # A message must never be able to point the reader at an arbitrary
            # object elsewhere in the bucket (mention results, backups, config).
            raise SegmentAccessError(
                "Refusing a segment key outside the configured segment prefix",
                detail=f"key_prefix={key[:40]!r}",
            )
        if ".." in key or key.startswith("/"):
            raise SegmentAccessError("Refusing a malformed segment key")
        return key

    def write(self, ref: SegmentRef, data: bytes) -> StorageDescriptor:
        if not data:
            raise SegmentAccessError("Refusing to write an empty segment")
        key = self.key_for(ref)
        digest = sha256_hex(data)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType="audio/ogg",
                ServerSideEncryption="AES256",
                Metadata={
                    "sha256": digest,
                    "station-id": ref.station_id,
                    "segment-id": ref.segment_id,
                },
            )
        except Exception as error:  # noqa: BLE001 - classified below
            raise UpstreamUnavailableError(
                "S3 segment upload failed", detail=str(error)[:400]
            ) from error
        return StorageDescriptor(
            backend="s3",
            path=None,
            bucket=self._bucket,
            key=key,
            sha256=digest,
            size_bytes=len(data),
        )

    def read(self, descriptor: StorageDescriptor) -> bytes:
        key = self._validated_key(descriptor)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            data = response["Body"].read()
        except Exception as error:  # noqa: BLE001 - classified below
            if _is_not_found(error):
                raise SegmentMissingError(f"Segment object not found: {key}") from error
            raise UpstreamUnavailableError(
                "S3 segment download failed", detail=str(error)[:400]
            ) from error
        if len(data) != descriptor.size_bytes:
            raise SegmentAccessError(
                "Segment size does not match the recorded size",
                detail=f"expected={descriptor.size_bytes} actual={len(data)}",
            )
        verify_digest(data, descriptor.sha256, context=f"s3://{self._bucket}/{key}")
        return data

    def delete(self, descriptor: StorageDescriptor) -> bool:
        key = self._validated_key(descriptor)
        try:
            self._s3.delete_object(Bucket=self._bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - classified below
            if _is_not_found(error):
                return False
            raise UpstreamUnavailableError(
                "S3 segment delete failed", detail=str(error)[:400]
            ) from error
        return True

    def exists(self, descriptor: StorageDescriptor) -> bool:
        try:
            key = self._validated_key(descriptor)
        except SegmentAccessError:
            return False
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - existence check only
            if _is_not_found(error):
                return False
            raise UpstreamUnavailableError(
                "S3 segment head failed", detail=str(error)[:400]
            ) from error
        return True


def _is_not_found(error: Exception) -> bool:
    """Whether a boto3 error means 'the object is not there'.

    boto3 raises `ClientError` with a code in the response, but `FakeS3` in the
    test suite raises a `NoSuchKey` class, so both shapes are handled.
    """
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
    if code in _NOT_FOUND_CODES:
        return True
    return type(error).__name__ in {"NoSuchKey", "NoSuchBucket", "404"}


__all__ = ["S3SegmentStore"]
