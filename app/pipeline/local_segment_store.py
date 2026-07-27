"""Filesystem segment store — the single-node default (ADR-002).

Security posture, in the order the checks run:

1. Identifiers are validated by :class:`SegmentRef` before any path is built,
   so ``..``, separators and NUL never reach ``Path``.
2. The candidate path is fully resolved (``Path.resolve`` follows symlinks) and
   must be relative to the resolved spool root. One check therefore rejects
   both traversal *and* symlink escape.
3. The final open uses ``O_NOFOLLOW`` where the platform provides it, and the
   opened file's ``st_dev``/``st_ino`` are compared against the ``lstat`` taken
   during validation. That closes the window in which a symlink is swapped in
   between validation and open (TOCTOU).
4. The SHA-256 recorded at write time is verified before the bytes are returned.

Writes are atomic: content goes to ``<name>.tmp``, is flushed and ``fsync``-ed,
then ``os.replace``-d onto the final name. A reader therefore never observes a
partially written segment.
"""
from __future__ import annotations

import contextlib
import logging
import os
import stat as stat_module
import tempfile
import time
from pathlib import Path

from .contracts import StorageDescriptor
from .errors import SegmentAccessError, SegmentMissingError
from .segment_store import SegmentRef, sha256_hex, verify_digest

logger = logging.getLogger(__name__)

#: Written files are readable by the owner and the group (the container user),
#: never world-readable: segments contain broadcast audio.
SEGMENT_FILE_MODE = 0o640
SEGMENT_DIR_MODE = 0o750


class LocalSegmentStore:
    """Segment store rooted at a single spool directory."""

    backend = "local"

    def __init__(self, root: Path | str) -> None:
        # strict=False: the root may not exist yet on first start.
        self._root = Path(root).expanduser().resolve(strict=False)

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        """Create the spool root. Called explicitly, never as a side effect."""
        self._root.mkdir(parents=True, exist_ok=True, mode=SEGMENT_DIR_MODE)

    # -- path safety -----------------------------------------------------------

    def _resolve_within_root(self, relative: str) -> Path:
        """Resolve ``relative`` under the root or refuse.

        ``resolve()`` follows symlinks, so a symlinked component that points
        outside the root produces a resolved path that fails
        ``is_relative_to`` — traversal and symlink escape are the same check.
        """
        candidate = (self._root / relative).resolve(strict=False)
        if candidate == self._root or not candidate.is_relative_to(self._root):
            raise SegmentAccessError(
                "Refusing a segment path outside the configured spool root",
                detail=f"relative={relative!r}",
            )
        return candidate

    def path_for(self, ref: SegmentRef) -> Path:
        return self._resolve_within_root(ref.relative_path)

    def _validated_read_path(self, descriptor: StorageDescriptor) -> Path:
        if descriptor.backend != "local":
            raise SegmentAccessError(
                f"LocalSegmentStore cannot read a {descriptor.backend!r} descriptor; "
                f"the producer and consumer disagree about RADIO_SEGMENT_STORE"
            )
        raw = str(descriptor.path or "")
        if not raw:
            raise SegmentAccessError("Local descriptor has no path")
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(self._root):
                raise SegmentAccessError(
                    "Refusing an absolute segment path outside the spool root"
                )
            return resolved
        return self._resolve_within_root(raw)

    # -- operations -----------------------------------------------------------

    def write(self, ref: SegmentRef, data: bytes) -> StorageDescriptor:
        if not data:
            raise SegmentAccessError("Refusing to write an empty segment")
        target = self.path_for(ref)
        target.parent.mkdir(parents=True, exist_ok=True, mode=SEGMENT_DIR_MODE)

        handle, temporary_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{ref.segment_id}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, SEGMENT_FILE_MODE)
            # Atomic within a filesystem: a reader sees either the old name or
            # the complete new file, never a partial write.
            os.replace(temporary, target)
        except Exception:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        self._fsync_directory(target.parent)

        return StorageDescriptor(
            backend="local",
            path=str(target),
            bucket=None,
            key=None,
            sha256=sha256_hex(data),
            size_bytes=len(data),
        )

    def read(self, descriptor: StorageDescriptor) -> bytes:
        path = self._validated_read_path(descriptor)
        try:
            link_stat = path.lstat()
        except FileNotFoundError as error:
            raise SegmentMissingError(f"Segment not found: {path.name}") from error
        except OSError as error:
            raise SegmentAccessError("Segment could not be inspected", detail=str(error)) from error

        if not stat_module.S_ISREG(link_stat.st_mode):
            # lstat does not follow symlinks, so a symlink is caught here before
            # the open below even runs.
            raise SegmentAccessError("Segment path is not a regular file")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor_fd = os.open(path, flags)
        except FileNotFoundError as error:
            raise SegmentMissingError(f"Segment not found: {path.name}") from error
        except OSError as error:
            # ELOOP here means the final component became a symlink between the
            # lstat above and this open.
            raise SegmentAccessError(
                "Segment could not be opened safely", detail=str(error)
            ) from error
        try:
            open_stat = os.fstat(descriptor_fd)
            if (open_stat.st_dev, open_stat.st_ino) != (link_stat.st_dev, link_stat.st_ino):
                raise SegmentAccessError(
                    "Segment was replaced between validation and open; refusing to read"
                )
            with os.fdopen(descriptor_fd, "rb") as stream:
                descriptor_fd = -1
                data = stream.read()
        finally:
            if descriptor_fd >= 0:
                os.close(descriptor_fd)

        if len(data) != descriptor.size_bytes:
            raise SegmentAccessError(
                "Segment size does not match the recorded size",
                detail=f"expected={descriptor.size_bytes} actual={len(data)}",
            )
        verify_digest(data, descriptor.sha256, context=f"segment {path.name}")
        return data

    def delete(self, descriptor: StorageDescriptor) -> bool:
        try:
            path = self._validated_read_path(descriptor)
        except SegmentAccessError:
            # Never widen deletion authority on a bad descriptor.
            raise
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SegmentAccessError("Segment could not be deleted", detail=str(error)) from error
        return True

    def exists(self, descriptor: StorageDescriptor) -> bool:
        try:
            return self._validated_read_path(descriptor).is_file()
        except SegmentAccessError:
            return False

    # -- maintenance ----------------------------------------------------------

    def usage_percent(self) -> float:
        """Disk usage of the filesystem holding the spool, 0..100."""
        try:
            usage = os.statvfs(self._root)
        except (AttributeError, OSError):
            # Windows development machines have no statvfs; report 0 rather
            # than pretending to know, and let the caller decide.
            return 0.0
        total = usage.f_blocks * usage.f_frsize
        if total <= 0:
            return 0.0
        free = usage.f_bavail * usage.f_frsize
        return round(100.0 * (total - free) / total, 2)

    def iter_segment_paths(self):
        """Yield every segment file currently on disk."""
        if not self._root.is_dir():
            return
        for station_dir in sorted(self._root.iterdir()):
            if not station_dir.is_dir():
                continue
            for candidate in sorted(station_dir.iterdir()):
                if candidate.is_file() and not candidate.name.startswith("."):
                    yield candidate

    def iter_stale_temporaries(self, older_than_seconds: float):
        """Yield abandoned ``.tmp`` files left by a crashed writer."""
        cutoff = time.time() - older_than_seconds
        if not self._root.is_dir():
            return
        for station_dir in sorted(self._root.iterdir()):
            if not station_dir.is_dir():
                continue
            for candidate in station_dir.glob(".*.tmp"):
                with contextlib.suppress(OSError):
                    if candidate.stat().st_mtime < cutoff:
                        yield candidate

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist the rename itself, not just the file contents."""
        if not hasattr(os, "O_DIRECTORY"):
            return  # Windows: not applicable, and not a production target.
        try:
            handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(handle)
        except OSError:
            logger.debug("Directory fsync unsupported for %s", directory)
        finally:
            os.close(handle)


__all__ = ["SEGMENT_DIR_MODE", "SEGMENT_FILE_MODE", "LocalSegmentStore"]
