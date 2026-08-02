"""Pipeline health, readiness and capacity reporting.

Everything here is cheap by construction: SQLite reads, one ``statvfs``, and
nothing else. Health endpoints must not open a station stream, load an ASR
model, generate an LLM token, or list a bucket -- a health check that does
expensive work fails under exactly the load it exists to detect, and turns a
busy system into an unhealthy-looking one.

The counters are kept distinct on purpose, because conflating them is how a
system starts claiming capacity it does not have:

``catalog_station_count``
    Stations known to the catalogue. Says nothing about load.
``campaign_station_reference_count``
    Campaign-to-station rows. Grows with campaigns, not with compute.
``unique_requested_station_count``
    Distinct stations at least one active campaign wants.
``unique_active_station_count``
    Distinct stations actually being listened to. **This is the load number.**
``pending_capacity_station_count``
    Wanted, but over the limit. Visible overflow, never a silent drop.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Settings
from ..pipeline.enums import SpoolPressure
from ..pipeline.heartbeat import HeartbeatReader

logger = logging.getLogger(__name__)

#: Roles a shared_sqs deployment needs before it can be called ready.
REQUIRED_ROLES: tuple[str, ...] = ("planner", "listener", "transcription", "analysis")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class PipelineStatusService:
    """Builds the pipeline sections of ``/healthz``, ``/readyz`` and monitoring."""

    def __init__(
        self,
        settings: Settings,
        database: Any,
        *,
        segment_store: Any | None = None,
        clock=None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._store = segment_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._heartbeats = HeartbeatReader(
            database, stale_after_seconds=settings.RADIO_HEARTBEAT_STALE_SECONDS
        )

    # -- top level -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The full pipeline status block. Safe to call on every request."""
        settings = self._settings
        heartbeats = self._heartbeats.snapshot(now=self._clock())
        roles = {
            role: self._heartbeats.role_status(role, now=self._clock())
            for role in REQUIRED_ROLES
        }
        pressure = self.spool_pressure()

        return {
            "pipeline_mode": "shared_sqs",
            "queue_backend": settings.RADIO_QUEUE_BACKEND,
            "segment_store": settings.RADIO_SEGMENT_STORE,
            "queues_configured": self.queues_configured(),
            "components": {
                "planner": roles["planner"],
                "listener": roles["listener"],
                "transcription_worker": roles["transcription"],
                "analysis_worker": roles["analysis"],
                "spool": pressure,
            },
            "listener_heartbeat": self._role_detail("listener", heartbeats),
            "transcription_worker_heartbeat": self._role_detail("transcription", heartbeats),
            "analysis_worker_heartbeat": self._role_detail("analysis", heartbeats),
            "planner_heartbeat": self._role_detail("planner", heartbeats),
            "queue_age_seconds": self.queue_age_seconds(),
            "spool_usage_percent": self.spool_usage_percent(),
            "spool_pressure": pressure,
            "outbox": self.outbox_stats(),
            "shard_coverage": self._heartbeats.shard_coverage(now=self._clock()),
            **self.capacity(),
        }

    def readiness(self) -> dict[str, Any]:
        """Whether this deployment can actually do its job right now.

        Readiness requires every worker role, because there is one pipeline and
        a missing role means audio is being captured and never transcribed --
        which looks healthy from the API and loses every mention.
        """
        checks: dict[str, str] = {"database": "ok" if self._database_ok() else "error"}

        for role in REQUIRED_ROLES:
            checks[role] = self._heartbeats.role_status(role, now=self._clock())
        checks["queues"] = "ok" if self.queues_configured() else "unconfigured"
        pressure = self.spool_pressure()
        checks["spool"] = pressure

        ready = (
            checks["database"] == "ok"
            and checks["queues"] == "ok"
            and all(checks[role] == "ok" for role in REQUIRED_ROLES)
            # A full spool cannot accept new audio, so the node is not ready to
            # take traffic even though every process is alive.
            and pressure != "emergency"
        )
        return {"ready": ready, "pipeline_mode": "shared_sqs", "checks": checks}

    # -- components ------------------------------------------------------------

    def _database_ok(self) -> bool:
        try:
            return bool(self._database.ping())
        except Exception:  # noqa: BLE001 - an unreachable database is not ready
            return False

    def queues_configured(self) -> bool:
        settings = self._settings
        if settings.RADIO_QUEUE_BACKEND == "memory":
            return True
        return bool(
            settings.RADIO_TRANSCRIPTION_QUEUE_URL.strip()
            and settings.RADIO_ANALYSIS_QUEUE_URL.strip()
        )

    def _role_detail(self, role: str, heartbeats: dict[str, Any]) -> dict[str, Any] | None:
        """The most recently seen worker for a role, without leaking detail."""
        candidates = [
            worker for worker in heartbeats["workers"] if worker["role"] == role
        ]
        if not candidates:
            return None
        newest = min(
            candidates,
            key=lambda worker: worker["age_seconds"]
            if worker["age_seconds"] is not None
            else float("inf"),
        )
        return {
            "status": newest["status"],
            "age_seconds": newest["age_seconds"],
            "stale": newest["stale"],
            "shard_index": newest["shard_index"],
            "shard_count": newest["shard_count"],
        }

    # -- queue and spool -------------------------------------------------------

    def queue_age_seconds(self) -> float | None:
        """Age of the oldest message still waiting to be sent or processed.

        Derived from the outbox and from job rows rather than by calling
        SQS: ``GetQueueAttributes`` on every health request is a per-request
        API cost, and the outbox is a strictly earlier signal anyway -- work
        backs up here before it backs up in the queue.
        """
        row = self._database.read_one(
            "SELECT min(created_at_utc) AS oldest FROM outbox_events WHERE status='pending'"
        )
        oldest = _parse(row["oldest"] if row else None)
        pending_job = self._database.read_one(
            "SELECT min(created_at_utc) AS oldest FROM transcription_jobs WHERE status='pending'"
        )
        job_oldest = _parse(pending_job["oldest"] if pending_job else None)

        candidates = [value for value in (oldest, job_oldest) if value is not None]
        if not candidates:
            return None
        return round(max(0.0, (self._clock() - min(candidates)).total_seconds()), 1)

    def spool_usage_percent(self) -> float:
        usage = getattr(self._store, "usage_percent", None)
        if not callable(usage):
            return 0.0
        try:
            return round(float(usage()), 2)
        except Exception:  # noqa: BLE001 - an unreadable spool is not a crash
            return 0.0

    def spool_pressure(self) -> SpoolPressure:
        percent = self.spool_usage_percent()
        settings = self._settings
        if percent >= settings.RADIO_SPOOL_EMERGENCY_PERCENT:
            return "emergency"
        if percent >= settings.RADIO_SPOOL_PAUSE_PERCENT:
            return "pause"
        if percent >= settings.RADIO_SPOOL_WARNING_PERCENT:
            return "warning"
        return "ok"

    def outbox_stats(self) -> dict[str, Any]:
        rows = self._database.read_all(
            "SELECT status, count(*) AS n FROM outbox_events GROUP BY status"
        )
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        return {
            "pending": counts.get("pending", 0),
            "sending": counts.get("sending", 0),
            "failed": counts.get("failed", 0),
        }

    # -- capacity --------------------------------------------------------------

    def capacity(self) -> dict[str, Any]:
        """Station counters, each with a distinct and documented meaning."""
        rows = self._database.read_all(
            "SELECT state, count(*) AS n FROM station_subscriptions GROUP BY state"
        )
        by_state = {str(row["state"]): int(row["n"]) for row in rows}
        active_states = {"starting", "active", "degraded", "winding_down"}

        references = self._database.read_one(
            "SELECT count(*) AS n FROM campaign_stations cs"
            " JOIN campaigns c ON c.id = cs.campaign_id WHERE c.status='active'"
        )
        requested = self._database.read_one(
            "SELECT count(*) AS n FROM station_subscriptions WHERE reference_count > 0"
        )
        shared = self._database.read_one(
            "SELECT count(*) AS n FROM station_subscriptions WHERE reference_count > 1"
        )

        return {
            "catalog_station_count": self._catalog_station_count(),
            "campaign_station_reference_count": int(references["n"]) if references else 0,
            "unique_requested_station_count": int(requested["n"]) if requested else 0,
            "unique_active_station_count": sum(
                by_state.get(state, 0) for state in active_states
            ),
            "pending_capacity_station_count": by_state.get("pending_capacity", 0),
            "reused_station_stream_count": int(shared["n"]) if shared else 0,
            "active_unique_station_limit": self._settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS,
            "worker_count": self._worker_count(),
            "listener_shard_count": self._settings.RADIO_LISTENER_SHARD_COUNT,
        }

    def _catalog_station_count(self) -> int:
        present = self._database.read_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_stations'"
        )
        if present is None:
            return 0
        row = self._database.read_one("SELECT count(*) AS n FROM managed_stations")
        return int(row["n"]) if row else 0

    def _worker_count(self) -> int:
        cutoff = (
            self._clock() - timedelta(seconds=self._settings.RADIO_HEARTBEAT_STALE_SECONDS)
        ).isoformat().replace("+00:00", "Z")
        row = self._database.read_one(
            "SELECT count(*) AS n FROM worker_heartbeats WHERE last_seen_utc >= ?"
            " AND status != 'stopped'",
            (cutoff,),
        )
        return int(row["n"]) if row else 0


__all__ = ["REQUIRED_ROLES", "PipelineStatusService"]
