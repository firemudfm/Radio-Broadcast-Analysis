"""Segment store path safety and integrity (ADR-002).

These are the tests that matter most in this module: a traversal or symlink
escape here would let a queue message read or delete an arbitrary file, and a
missing checksum check would launder corrupted audio into the evidence record.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.pipeline.contracts import StorageDescriptor
from app.pipeline.errors import ChecksumMismatchError, SegmentAccessError, SegmentMissingError
from app.pipeline.local_segment_store import LocalSegmentStore
from app.pipeline.s3_segment_store import S3SegmentStore
from app.pipeline.segment_store import SegmentRef, sha256_hex

SEGMENT_ID = "11111111-1111-4111-8111-111111111111"
PAYLOAD = b"OggS-fake-opus-payload" * 32


def _ref(station: str = "rb-abc123") -> SegmentRef:
    return SegmentRef(station_id=station, segment_id=SEGMENT_ID)


# --- identity validation ------------------------------------------------------


@pytest.mark.parametrize(
    "station_id",
    ["../etc", "a/b", "a\\b", "", "..", "a" * 200, "sta tion", "st;id"],
)
def test_segment_ref_rejects_unsafe_station_ids(station_id: str) -> None:
    with pytest.raises(ValueError):
        SegmentRef(station_id=station_id, segment_id=SEGMENT_ID)


def test_segment_ref_rejects_a_non_uuid_segment_id() -> None:
    with pytest.raises(ValueError):
        SegmentRef(station_id="rb-abc", segment_id="../../evil")


def test_segment_ref_rejects_an_unknown_extension() -> None:
    with pytest.raises(ValueError, match="Unsupported segment extension"):
        SegmentRef(station_id="rb-abc", segment_id=SEGMENT_ID, extension="sh")


# --- round trip ---------------------------------------------------------------


def test_write_read_delete_round_trip(spool: LocalSegmentStore) -> None:
    descriptor = spool.write(_ref(), PAYLOAD)
    assert descriptor.backend == "local"
    assert descriptor.sha256 == sha256_hex(PAYLOAD)
    assert descriptor.size_bytes == len(PAYLOAD)
    assert spool.exists(descriptor)
    assert spool.read(descriptor) == PAYLOAD
    assert spool.delete(descriptor) is True
    assert spool.delete(descriptor) is False
    assert spool.exists(descriptor) is False


def test_write_is_atomic_and_leaves_no_temporaries(spool: LocalSegmentStore) -> None:
    descriptor = spool.write(_ref(), PAYLOAD)
    directory = Path(descriptor.path).parent
    assert not list(directory.glob("*.tmp"))
    assert not list(directory.glob(".*.tmp"))


def test_empty_segments_are_refused(spool: LocalSegmentStore) -> None:
    with pytest.raises(SegmentAccessError, match="empty segment"):
        spool.write(_ref(), b"")


def test_written_file_is_not_world_readable(spool: LocalSegmentStore) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    descriptor = spool.write(_ref(), PAYLOAD)
    mode = Path(descriptor.path).stat().st_mode & 0o777
    assert mode & 0o007 == 0, f"segment audio is world-accessible: {oct(mode)}"


# --- path safety --------------------------------------------------------------


def test_empty_path_is_refused_by_the_descriptor_itself(spool: LocalSegmentStore) -> None:
    """Caught one layer earlier, at schema validation, before any path is built."""
    with pytest.raises(ValueError, match="local storage requires"):
        StorageDescriptor(
            backend="local",
            path="",
            bucket=None,
            key=None,
            sha256=sha256_hex(PAYLOAD),
            size_bytes=len(PAYLOAD),
        )


@pytest.mark.parametrize(
    "hostile_path",
    [
        "../../../etc/passwd",
        "rb-abc/../../../etc/passwd",
        "rb-abc/../../outside.opus",
        "rb-abc/../../../../../../../../etc/shadow",
    ],
)
def test_traversal_paths_are_refused(spool: LocalSegmentStore, hostile_path: str) -> None:
    descriptor = StorageDescriptor(
        backend="local",
        path=hostile_path,
        bucket=None,
        key=None,
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    with pytest.raises(SegmentAccessError):
        spool.read(descriptor)


def test_absolute_path_outside_the_root_is_refused(spool: LocalSegmentStore, tmp_path: Path) -> None:
    outsider = tmp_path / "outside.opus"
    outsider.write_bytes(PAYLOAD)
    descriptor = StorageDescriptor(
        backend="local",
        path=str(outsider),
        bucket=None,
        key=None,
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    with pytest.raises(SegmentAccessError, match="outside the spool root"):
        spool.read(descriptor)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_symlink_escape_is_refused(spool: LocalSegmentStore, tmp_path: Path) -> None:
    """resolve() follows symlinks, so escape and traversal are one check."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"private")
    station_dir = spool.root / "rb-abc123"
    station_dir.mkdir(parents=True, exist_ok=True)
    link = station_dir / f"{SEGMENT_ID}.opus"
    os.symlink(secret, link)

    descriptor = StorageDescriptor(
        backend="local",
        path=str(link),
        bucket=None,
        key=None,
        sha256=sha256_hex(b"private"),
        size_bytes=7,
    )
    with pytest.raises(SegmentAccessError):
        spool.read(descriptor)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_symlinked_station_directory_is_refused(spool: LocalSegmentStore, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / f"{SEGMENT_ID}.opus").write_bytes(PAYLOAD)
    os.symlink(outside, spool.root / "rb-linked")

    descriptor = StorageDescriptor(
        backend="local",
        path=f"rb-linked/{SEGMENT_ID}.opus",
        bucket=None,
        key=None,
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    with pytest.raises(SegmentAccessError):
        spool.read(descriptor)


def test_delete_refuses_a_hostile_descriptor(spool: LocalSegmentStore, tmp_path: Path) -> None:
    """A bad descriptor must never widen deletion authority."""
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    descriptor = StorageDescriptor(
        backend="local",
        path=str(victim),
        bucket=None,
        key=None,
        sha256=sha256_hex(b"keep me"),
        size_bytes=7,
    )
    with pytest.raises(SegmentAccessError):
        spool.delete(descriptor)
    assert victim.exists()


# --- integrity ----------------------------------------------------------------


def test_checksum_mismatch_is_permanent(spool: LocalSegmentStore) -> None:
    descriptor = spool.write(_ref(), PAYLOAD)
    Path(descriptor.path).write_bytes(b"t" * len(PAYLOAD))
    error = pytest.raises(ChecksumMismatchError, spool.read, descriptor)
    assert error.value.retryable is False


def test_size_mismatch_is_detected(spool: LocalSegmentStore) -> None:
    descriptor = spool.write(_ref(), PAYLOAD)
    Path(descriptor.path).write_bytes(PAYLOAD[:-10])
    with pytest.raises(SegmentAccessError, match="size does not match"):
        spool.read(descriptor)


def test_missing_segment_is_permanent(spool: LocalSegmentStore) -> None:
    descriptor = spool.write(_ref(), PAYLOAD)
    Path(descriptor.path).unlink()
    error = pytest.raises(SegmentMissingError, spool.read, descriptor)
    assert error.value.retryable is False


# --- backend mismatch ---------------------------------------------------------


def test_local_store_refuses_an_s3_descriptor(spool: LocalSegmentStore) -> None:
    descriptor = StorageDescriptor(
        backend="s3",
        path=None,
        bucket="bucket",
        key="temp-speech/rb-abc/x.opus",
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    with pytest.raises(SegmentAccessError, match="disagree about RADIO_SEGMENT_STORE"):
        spool.read(descriptor)


class _FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_kwargs: list[dict] = []

    def put_object(self, **kwargs):
        self.put_kwargs.append(kwargs)
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])

    def get_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket, Key):
        del Bucket
        self.objects.pop(Key, None)

    def head_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        return {"ContentLength": len(self.objects[Key])}


def test_s3_store_round_trip_and_encryption() -> None:
    client = _FakeS3()
    store = S3SegmentStore(client, "bucket", "temp-speech/")
    descriptor = store.write(_ref(), PAYLOAD)
    assert descriptor.backend == "s3"
    assert descriptor.key == f"temp-speech/rb-abc123/{SEGMENT_ID}.opus"
    assert store.read(descriptor) == PAYLOAD
    assert client.put_kwargs[0]["ServerSideEncryption"] == "AES256"


def test_s3_store_refuses_a_key_outside_its_prefix() -> None:
    """A message must not be able to point the reader at mention results."""
    store = S3SegmentStore(_FakeS3(), "bucket", "temp-speech/")
    descriptor = StorageDescriptor(
        backend="s3",
        path=None,
        bucket="bucket",
        key="mentions/2026/07/27/secret/analysis.json",
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    with pytest.raises(SegmentAccessError, match="outside the configured segment prefix"):
        store.read(descriptor)


def test_s3_store_refuses_a_foreign_bucket() -> None:
    store = S3SegmentStore(_FakeS3(), "bucket", "temp-speech/")
    descriptor = StorageDescriptor(
        backend="s3",
        path=None,
        bucket="attacker-bucket",
        key="temp-speech/rb-abc/x.opus",
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    with pytest.raises(SegmentAccessError, match="different bucket"):
        store.read(descriptor)


def test_s3_missing_object_is_permanent() -> None:
    store = S3SegmentStore(_FakeS3(), "bucket", "temp-speech/")
    descriptor = StorageDescriptor(
        backend="s3",
        path=None,
        bucket="bucket",
        key="temp-speech/rb-abc/gone.opus",
        sha256=sha256_hex(PAYLOAD),
        size_bytes=len(PAYLOAD),
    )
    error = pytest.raises(SegmentMissingError, store.read, descriptor)
    assert error.value.retryable is False


def test_both_backends_produce_equivalent_descriptors(spool: LocalSegmentStore) -> None:
    """Round-trip parity: the same bytes, the same digest, either way."""
    s3_store = S3SegmentStore(_FakeS3(), "bucket", "temp-speech/")
    local_descriptor = spool.write(_ref(), PAYLOAD)
    s3_descriptor = s3_store.write(_ref(), PAYLOAD)
    assert local_descriptor.sha256 == s3_descriptor.sha256
    assert local_descriptor.size_bytes == s3_descriptor.size_bytes
    assert spool.read(local_descriptor) == s3_store.read(s3_descriptor) == PAYLOAD
