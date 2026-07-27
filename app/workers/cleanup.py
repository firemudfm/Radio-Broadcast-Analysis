"""Cleanup worker: spool retention and disk back-pressure.

Deleting audio is the one irreversible thing this system does, so the rule is
strict: **a file is only removed when SQLite says it is safe.** Age alone is
never sufficient. A segment can be minutes old and still be queued behind a slow
transcription worker; deleting it by age would destroy a mention that was about
to be found.

Every deletion therefore joins against job state:

* ``disposable`` -- transcribed, matched nothing, past the no-hit retention;
* ``failed`` -- past the failure retention, kept longer for diagnosis;
* ``retained`` -- part of a mention. Never deleted here.
* ``pending`` -- not yet transcribed. Never deleted here, at any watermark.

Watermarks escalate rather than switch: warning logs and degrades health, pause
stops admitting new segments (enforced in the listener), emergency additionally
sweeps everything already expired. Even at emergency the worker will not touch
an in-flight segment or a confirmed mention -- it would rather report a full
disk than delete evidence.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from ..observability import log_fields, safe_extra
from ..pipeline.contracts import StorageDescriptor
from ..pipeline.enums import SpoolPressure
from ..pipeline.factory import build_segment_store
from ..pipeline.idempotency import InboxGuard
from . import BaseWorker, bootstrap

logger = logging.getLogger(__name__)

#: Deleted per cycle. Bounded so cleanup cannot monopolise the write lock.
DELETE_BATCH = 200


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CleanupWorker(BaseWorker):
    role = "cleanup"

    def __init__(self, settings, database, *, segment_store=None, **kwargs) -> None:
        super().__init__(settings, database, **kwargs)
        self.store = segment_store or build_segment_store(settings)
        self.idle_sleep_seconds = settings.RADIO_CLEANUP_POLL_SECONDS
        self.stats = {"deleted": 0, "reclaimed_bytes": 0, "orphans": 0}

    def tick(self) -> bool:
        pressure = self.pressure()
        removed = self.sweep(pressure=pressure)
        if pressure != "ok":
            logger.warning(
                "Spool is above a watermark",
                extra=safe_extra(
                    {
                        "spool_pressure": pressure,
                        "spool_usage_percent": self.usage_percent(),
                        "deleted": removed,
                    }
                ),
            )
        self.beat(
            status="degraded" if pressure in {"pause", "emergency"} else "ok",
            detail={
                "spool_pressure": pressure,
                "spool_usage_percent": self.usage_percent(),
                **self.stats,
            },
        )
        return removed == 0

    # -- watermarks ------------------------------------------------------------

    def usage_percent(self) -> float:
        usage = getattr(self.store, "usage_percent", None)
        return round(usage(), 2) if callable(usage) else 0.0

    def pressure(self) -> SpoolPressure:
        percent = self.usage_percent()
        settings = self.settings
        if percent >= settings.RADIO_SPOOL_EMERGENCY_PERCENT:
            return "emergency"
        if percent >= settings.RADIO_SPOOL_PAUSE_PERCENT:
            return "pause"
        if percent >= settings.RADIO_SPOOL_WARNING_PERCENT:
            return "warning"
        return "ok"

    # -- sweeping --------------------------------------------------------------

    def sweep(self, *, pressure: SpoolPressure = "ok") -> int:
        """Delete expired segments whose job state proves they are safe."""
        now = datetime.now(UTC)
        settings = self.settings

        no_hit_cutoff = _iso(now - timedelta(minutes=settings.RADIO_NO_HIT_RETENTION_MINUTES))
        failed_cutoff = _iso(now - timedelta(hours=settings.RADIO_FAILED_SEGMENT_RETENTION_HOURS))
        if pressure == "emergency":
            # Bring expiry forward, but never past "still needed": the
            # disposition and job-status predicates below are unchanged.
            no_hit_cutoff = _iso(now)

        candidates = self.database.read_all(
            """
            SELECT s.segment_id, s.storage_backend, s.storage_path, s.storage_bucket,
                   s.storage_key, s.sha256, s.size_bytes, s.disposition
            FROM audio_segments s
            LEFT JOIN transcription_jobs j ON j.segment_id = s.segment_id
            WHERE (
                    (s.disposition = 'disposable' AND s.updated_at_utc < ?
                     AND j.status IN ('succeeded', 'abandoned'))
                 OR (s.disposition = 'failed' AND s.updated_at_utc < ?)
                  )
              -- Belt and braces: never touch audio a mention depends on.
              AND s.disposition NOT IN ('retained', 'pending')
              AND NOT EXISTS (
                    SELECT 1 FROM transcripts t
                    JOIN mention_events m ON m.transcript_id = t.transcript_id
                    WHERE t.segment_id = s.segment_id
                  )
            ORDER BY s.updated_at_utc
            LIMIT ?
            """,
            (no_hit_cutoff, failed_cutoff, DELETE_BATCH),
        )

        deleted = 0
        for row in candidates:
            if self.should_stop:
                break
            if self._delete_segment(row):
                deleted += 1
        if deleted:
            self.stats["deleted"] += deleted
            logger.info("Reclaimed spool space", extra=log_fields(deleted=deleted))
        self._prune_tables()
        return deleted

    def _delete_segment(self, row) -> bool:
        try:
            descriptor = StorageDescriptor(
                backend=str(row["storage_backend"]),
                path=row["storage_path"],
                bucket=row["storage_bucket"],
                key=row["storage_key"],
                sha256=str(row["sha256"]),
                size_bytes=int(row["size_bytes"]),
            )
        except Exception:  # noqa: BLE001 - an unreadable row is not a reason to stop
            logger.warning(
                "Skipping a segment with an invalid storage descriptor",
                extra=log_fields(segment_id=str(row["segment_id"])),
            )
            return False

        with contextlib.suppress(Exception):
            # An already-absent object is success, not failure.
            self.store.delete(descriptor)

        stamp = _iso(datetime.now(UTC))
        segment_id = str(row["segment_id"])
        self.database.write(
            lambda connection: connection.execute(
                "UPDATE audio_segments SET disposition='deleted', storage_path=NULL,"
                " storage_key=NULL, updated_at_utc=? WHERE segment_id=?",
                (stamp, segment_id),
            )
        )
        self.stats["reclaimed_bytes"] += int(row["size_bytes"] or 0)
        return True

    def _prune_tables(self) -> None:
        """Bounded retention for the inbox and for deleted segment rows."""
        settings = self.settings
        try:
            for queue_name in ("transcription", "analysis"):
                InboxGuard(self.database, queue_name).prune(
                    retention_days=settings.RADIO_INBOX_RETENTION_DAYS
                )
            cutoff = _iso(
                datetime.now(UTC) - timedelta(days=settings.RADIO_EVIDENCE_RETENTION_DAYS)
            )

            def prune(connection: sqlite3.Connection) -> None:
                connection.execute(
                    "DELETE FROM audio_segments WHERE disposition='deleted'"
                    " AND updated_at_utc < ?",
                    (cutoff,),
                )

            self.database.write(prune)
        except Exception:  # noqa: BLE001 - housekeeping must not stop the worker
            logger.warning("Table pruning failed", extra=log_fields(worker_id=self.worker_id))


def main() -> None:
    settings, database = bootstrap("cleanup")
    try:
        CleanupWorker(settings, database).run()
    finally:
        database.close()


if __name__ == "__main__":
    main()


__all__ = ["DELETE_BATCH", "CleanupWorker", "main"]
