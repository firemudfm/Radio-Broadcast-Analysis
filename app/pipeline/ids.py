"""Identifier validation and deterministic hashing.

Two rules live here because getting either wrong is a silent, expensive bug:

1. Identifiers that reach a filesystem path, a systemd unit name or an SQS
   ``MessageGroupId`` are validated against a strict pattern *before* use, not
   sanitised afterwards. Sanitising is a guessing game; rejecting is not.

2. Shard assignment uses BLAKE2b, never Python's built-in ``hash()``.
   ``hash()`` of a ``str`` is randomised per process by ``PYTHONHASHSEED``, so
   two containers would disagree about which stations they own — a split brain
   that either double-connects a station or drops it entirely, with no error.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid

#: Station ids come from two sources: catalogue ids (``rb-<uuid>``) and legacy
#: pipeline ids (``hertz879``). Both fit this pattern. Deliberately excludes
#: ``.``, ``/``, ``\``, whitespace and NUL, so a station id can never traverse a
#: path or terminate a shell word.
STATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,126}$")

#: SQS caps MessageGroupId at 128 characters.
MAX_MESSAGE_GROUP_ID_LENGTH = 128

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class IdentifierError(ValueError):
    """An identifier failed validation and must not be used."""


def validate_station_id(value: str) -> str:
    """Return ``value`` if it is a safe station identifier, else raise.

    Safe means: usable as a path segment, a MessageGroupId and a log field
    without escaping.
    """
    candidate = str(value or "").strip()
    if not candidate:
        raise IdentifierError("station_id must not be empty")
    if len(candidate) > MAX_MESSAGE_GROUP_ID_LENGTH:
        raise IdentifierError(
            f"station_id exceeds the {MAX_MESSAGE_GROUP_ID_LENGTH}-character MessageGroupId limit"
        )
    if not STATION_ID_PATTERN.fullmatch(candidate):
        raise IdentifierError(
            f"station_id {candidate!r} contains characters that are not allowed "
            f"(letters, digits, hyphen and underscore only)"
        )
    return candidate


def validate_uuid(value: str, *, field: str = "id") -> str:
    """Return the canonical lower-case form of a UUID string, else raise."""
    candidate = str(value or "").strip()
    if not _UUID_PATTERN.fullmatch(candidate):
        raise IdentifierError(f"{field} must be a UUID, got {candidate!r}")
    return candidate.lower()


def new_id() -> str:
    """A fresh random identifier (uuid4) as a canonical lower-case string."""
    return str(uuid.uuid4())


def stable_hash(value: str) -> int:
    """Process-independent 64-bit hash of ``value``.

    BLAKE2b with an 8-byte digest. Stable across processes, hosts, Python
    versions and ``PYTHONHASHSEED`` — which is the entire point.
    """
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def stable_shard_index(station_id: str, shard_count: int) -> int:
    """Deterministic shard for ``station_id`` across ``shard_count`` shards.

    Every process computes the same answer for the same inputs, which is what
    makes it safe for several listener containers to divide stations between
    themselves with no coordination.
    """
    if shard_count < 1:
        raise IdentifierError("shard_count must be at least 1")
    return stable_hash(validate_station_id(station_id)) % shard_count


def owns_station(station_id: str, *, shard_count: int, shard_index: int) -> bool:
    """Whether the shard identified by ``shard_index`` should run this station."""
    if not 0 <= shard_index < shard_count:
        raise IdentifierError("shard_index must satisfy 0 <= index < count")
    return stable_shard_index(station_id, shard_count) == shard_index


def content_fingerprint(*parts: str) -> str:
    """Stable SHA-256 hex digest over ordered parts.

    Used for keyword-index versioning: the index is republished only when this
    value changes, so an edit that does not alter effective content does not
    churn every listener.

    Parts are NUL-separated (a byte that cannot occur in the validated inputs)
    so that ``("ab", "c")`` and ``("a", "bc")`` cannot collide.
    """
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(str(part).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def normalize_identifier_text(value: str) -> str:
    """NFKC-normalise and casefold text used to build deterministic ids.

    NFKC (not NFKD) because the result is an identifier, not a matching key:
    composed forms keep ids shorter and stable across input methods.
    """
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


__all__ = [
    "IdentifierError",
    "MAX_MESSAGE_GROUP_ID_LENGTH",
    "STATION_ID_PATTERN",
    "content_fingerprint",
    "new_id",
    "normalize_identifier_text",
    "owns_station",
    "stable_hash",
    "stable_shard_index",
    "validate_station_id",
    "validate_uuid",
]
