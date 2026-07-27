"""Segment storage abstraction (ADR-002).

The store moves audio between the listener and the transcription worker. Which
backend is in use is recorded *in the message*, never inferred by the reader, so
that a message written in one mode cannot be misread in another.

Every read verifies the SHA-256 recorded at write time before the bytes reach a
decoder. Corruption and tampering therefore fail closed instead of being
laundered into the evidence record.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .contracts import StorageDescriptor
from .errors import ChecksumMismatchError
from .ids import validate_station_id, validate_uuid

#: Chunk size for streaming digests. Large enough to be efficient, small enough
#: that a corrupt multi-megabyte file never lands in memory whole.
DIGEST_CHUNK_BYTES = 1024 * 1024

#: Segment filenames are always ``<uuid>.<ext>`` with an allowlisted extension.
SEGMENT_FILENAME_PATTERN = re.compile(r"^[0-9a-f-]{36}\.(opus|wav|flac)$")

ALLOWED_SEGMENT_EXTENSIONS: frozenset[str] = frozenset({"opus", "wav", "flac"})


@dataclass(frozen=True)
class SegmentRef:
    """Identity of a segment, independent of where its bytes live."""

    station_id: str
    segment_id: str
    extension: str = "opus"

    def __post_init__(self) -> None:
        validate_station_id(self.station_id)
        validate_uuid(self.segment_id, field="segment_id")
        if self.extension not in ALLOWED_SEGMENT_EXTENSIONS:
            raise ValueError(
                f"Unsupported segment extension {self.extension!r}; "
                f"allowed: {sorted(ALLOWED_SEGMENT_EXTENSIONS)}"
            )

    @property
    def filename(self) -> str:
        return f"{self.segment_id}.{self.extension}"

    @property
    def relative_path(self) -> str:
        """``<station_id>/<segment_id>.<ext>`` — identical in both backends.

        Both components are validated, so this string cannot contain ``..``, a
        leading separator, or any path-significant character.
        """
        return f"{self.station_id}/{self.filename}"


@runtime_checkable
class SegmentStore(Protocol):
    """Write-once, read-many, delete-once storage for audio segments."""

    backend: str

    def write(self, ref: SegmentRef, data: bytes) -> StorageDescriptor:
        """Store ``data`` atomically and return its self-describing locator."""

    def read(self, descriptor: StorageDescriptor) -> bytes:
        """Return the bytes, having verified the recorded digest."""

    def delete(self, descriptor: StorageDescriptor) -> bool:
        """Remove the object. Returns False when it was already gone."""

    def exists(self, descriptor: StorageDescriptor) -> bool:
        """Whether the object is currently retrievable."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_digest(data: bytes, expected_sha256: str, *, context: str) -> None:
    """Raise :class:`ChecksumMismatchError` unless ``data`` matches.

    Permanent by classification: retrying a read of bytes that do not match
    what the producer wrote will not make them match.
    """
    actual = sha256_hex(data)
    if actual != expected_sha256.strip().lower():
        raise ChecksumMismatchError(
            f"Checksum mismatch for {context}",
            detail=f"expected={expected_sha256[:16]}… actual={actual[:16]}…",
        )


__all__ = [
    "ALLOWED_SEGMENT_EXTENSIONS",
    "DIGEST_CHUNK_BYTES",
    "SEGMENT_FILENAME_PATTERN",
    "SegmentRef",
    "SegmentStore",
    "sha256_hex",
    "verify_digest",
]
