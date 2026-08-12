"""Worker heartbeats and stale-job recovery (ADR-009 §4-§5).

Heartbeats answer "is this role alive?" without asking the process itself —
`/healthz` reads the table, so a wedged worker that still holds its socket looks
stale rather than healthy.

Stale-job recovery catches what SQS cannot: a worker killed *after*
``ReceiveMessage`` succeeded but before any progress became durable. SQS will
redeliver on visibility expiry, but the job row would otherwise sit in
``running`` forever and block its own idempotency check.
"""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Job tables that carry a lease and can therefore be swept.
LEASED_JOB_TABLES: tuple[tuple[str, str], ...] = (
    ("transcription_jobs", "transcription_job_id"),
    ("analysis_jobs", "analysis_job_id"),
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def default_worker_id(role: str, shard_index: int = 0) -> str:
    """Stable per-container identity.

    Uses the hostname, which under Docker is the container id — so a restarted
    container reuses its row rather than leaking a new one on every restart.
    """
    return f"{role}-{shard_index}-{socket.gethostname()}"


class HeartbeatWriter:
    """Upserts one row per worker."""

    def __init__(
        self,
        database: Any,
        *,
        worker_id: str,
        role: str,
        shard_index: int = 0,
        shard_count: int = 1,
        pipeline_mode: str = "legacy",
    ) -> None:
        self._database = database
        self._worker_id = worker_id
        self._role = role
        self._shard_index = shard_index
        self._shard_count = shard_count
        self._pipeline_mode = pipeline_mode
        self._started_at = _iso(datetime.now(UTC))

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def beat(
        self,
        *,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        stamp = _iso(now or datetime.now(UTC))
        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)[:4000]

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO worker_heartbeats(
                  worker_id, role, shard_index, shard_count, status, detail_json,
                  pipeline_mode, started_at_utc, last_seen_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                  role=excluded.role,
                  shard_index=excluded.shard_index,
                  shard_count=excluded.shard_count,
                  status=excluded.status,
                  detail_json=excluded.detail_json,
                  pipeline_mode=excluded.pipeline_mode,
                  last_seen_utc=excluded.last_seen_utc
                """,
                (
                    self._worker_id,
                    self._role,
                    self._shard_index,
                    self._shard_count,
                    status,
                    payload,
                    self._pipeline_mode,
                    self._started_at,
                    stamp,
                ),
            )

        self._database.write(write)

    def stop(self, *, reason: str = "shutdown") -> None:
        """Mark the worker stopped so health does not wait for staleness."""
        self.beat(status="stopped", detail={"reason": reason})


class HeartbeatReader:
    """Reads heartbeat state for health, readiness and shard-coverage checks."""

    def __init__(self, database: Any, *, stale_after_seconds: int = 120) -> None:
        self._database = database
        self._stale_after = stale_after_seconds

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        rows = self._database.read_all(
            "SELECT worker_id, role, shard_index, shard_count, status, pipeline_mode,"
            " last_seen_utc FROM worker_heartbeats ORDER BY role, worker_id"
        )
        roles: dict[str, dict[str, Any]] = {}
        workers: list[dict[str, Any]] = []
        for row in rows:
            seen = _parse(row["last_seen_utc"])
            age = (reference - seen).total_seconds() if seen else None
            stale = age is None or age > self._stale_after
            record = {
                "worker_id": str(row["worker_id"]),
                "role": str(row["role"]),
                "shard_index": int(row["shard_index"]),
                "shard_count": int(row["shard_count"]),
                "status": str(row["status"]),
                "pipeline_mode": str(row["pipeline_mode"]),
                "age_seconds": round(age, 1) if age is not None else None,
                "stale": stale,
            }
            workers.append(record)
            bucket = roles.setdefault(record["role"], {"total": 0, "live": 0, "stale": 0})
            bucket["total"] += 1
            if stale or record["status"] == "stopped":
                bucket["stale"] += 1
            else:
                bucket["live"] += 1
        return {"roles": roles, "workers": workers}

    def role_status(self, role: str, *, now: datetime | None = None) -> str:
        """``ok`` when at least one live worker holds the role, else ``stale``/``absent``."""
        summary = self.snapshot(now=now)["roles"].get(role)
        if summary is None:
            return "absent"
        return "ok" if summary["live"] > 0 else "stale"

    def shard_coverage(self, *, role: str = "listener", now: datetime | None = None) -> dict[str, Any]:
        """Detect missing or duplicated listener shards.

        Both are silent failures otherwise: a missing shard drops its stations
        with no error, and a duplicate double-connects them.
        """
        workers = [
            worker
            for worker in self.snapshot(now=now)["workers"]
            if worker["role"] == role and not worker["stale"] and worker["status"] != "stopped"
        ]
        if not workers:
            return {"expected": 0, "covered": [], "missing": [], "duplicated": [], "healthy": False}
        expected = max(worker["shard_count"] for worker in workers)
        seen: dict[int, int] = {}
        for worker in workers:
            seen[worker["shard_index"]] = seen.get(worker["shard_index"], 0) + 1
        covered = sorted(seen)
        missing = [index for index in range(expected) if index not in seen]
        duplicated = sorted(index for index, count in seen.items() if count > 1)
        return {
            "expected": expected,
            "covered": covered,
            "missing": missing,
            "duplicated": duplicated,
            "healthy": not missing and not duplicated,
        }

    def prune(self, *, older_than_days: int = 7, now: datetime | None = None) -> int:
        cutoff = _iso((now or datetime.now(UTC)) - timedelta(days=older_than_days))

        def delete(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM worker_heartbeats WHERE last_seen_utc < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)

        return self._database.write(delete)


class StaleJobSweeper:
    """Returns leased jobs whose worker died back to ``pending``."""

    #: Pending transcription jobs older than this are orphans: SQS FIFO
    #: retains a message for at most 4 days, so a pending row this old has no
    #: message left to deliver -- no worker will ever receive it, and the
    #: receive-side stale-skip can never fire. Production accumulated ~20k of
    #: these from a backlog that outlived retention. Aligned to retention, not
    #: to the receive-side 6-hour skip: a younger row's message may still
    #: arrive, and the skip handles it there.
    ORPHAN_PENDING_HOURS = 96

    def __init__(self, database: Any, *, max_attempts: int = 5) -> None:
        self._database = database
        self._max_attempts = max_attempts

    def sweep(self, *, now: datetime | None = None) -> dict[str, int]:
        moment = now or datetime.now(UTC)
        reference = _iso(moment)
        results: dict[str, int] = {}
        for table, _id_column in LEASED_JOB_TABLES:
            results[table] = self._sweep_table(table, reference)
        results["orphaned_pending"] = self._sweep_orphaned_pending(moment)
        return results

    def _sweep_orphaned_pending(self, now: datetime) -> int:
        """Abandon pending transcription jobs whose message outlived SQS.

        Transcription only: an analysis job is a mention waiting to exist and
        is never discarded by age here. The orphan's segment is released to
        cleanup unless it is retained evidence.
        """
        stamp = _iso(now)
        cutoff = _iso(now - timedelta(hours=self.ORPHAN_PENDING_HOURS))

        def sweep(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "UPDATE transcription_jobs SET status='abandoned',"
                " last_error_code='message_retention_expired', updated_at_utc=?"
                " WHERE status='pending' AND created_at_utc <= ?",
                (stamp, cutoff),
            )
            abandoned = int(cursor.rowcount or 0)
            if abandoned:
                connection.execute(
                    "UPDATE audio_segments SET disposition='disposable',"
                    " updated_at_utc=?"
                    " WHERE disposition NOT IN ('retained', 'deleted')"
                    " AND segment_id IN (SELECT segment_id FROM transcription_jobs"
                    "   WHERE status='abandoned'"
                    "   AND last_error_code='message_retention_expired')",
                    (stamp,),
                )
            return abandoned

        abandoned = self._database.write(sweep)
        if abandoned:
            logger.warning(
                "Abandoned %d orphaned pending transcription jobs older than %dh"
                " (SQS retention outlived); their audio is released to cleanup",
                abandoned,
                self.ORPHAN_PENDING_HOURS,
            )
        return abandoned

    def _sweep_table(self, table: str, reference: str) -> int:
        def sweep(connection: sqlite3.Connection) -> int:
            # Abandon first: a job that has already burned its attempts must not
            # be handed back to a worker to fail again.
            connection.execute(
                f"UPDATE {table} SET status='abandoned', lease_expires_at_utc=NULL,"  # nosec B608 (table from a fixed tuple)
                " last_error_code='lease_exhausted', updated_at_utc=?"
                " WHERE status='running' AND lease_expires_at_utc IS NOT NULL"
                " AND lease_expires_at_utc <= ? AND attempts >= ?",
                (reference, reference, self._max_attempts),
            )
            cursor = connection.execute(
                f"UPDATE {table} SET status='pending', lease_expires_at_utc=NULL,"  # nosec B608 (table from a fixed tuple)
                " worker_id=NULL, updated_at_utc=?"
                " WHERE status='running' AND lease_expires_at_utc IS NOT NULL"
                " AND lease_expires_at_utc <= ? AND attempts < ?",
                (reference, reference, self._max_attempts),
            )
            return int(cursor.rowcount or 0)

        reclaimed = self._database.write(sweep)
        if reclaimed:
            logger.warning("Reclaimed %d stale jobs from %s", reclaimed, table)
        return reclaimed


__all__ = [
    "LEASED_JOB_TABLES",
    "HeartbeatReader",
    "HeartbeatWriter",
    "StaleJobSweeper",
    "default_worker_id",
]
