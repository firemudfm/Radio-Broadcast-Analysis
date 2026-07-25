"""SQLite store for the v0.4 radio catalogue and managed-station lifecycle.

This module layers new tables on top of the existing v0.3 Database without
changing its schema. All DDL is idempotent so upgrades and re-runs are safe.
`stream_url_resolved` lives only in this table and is never serialized into
public API responses.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import Database, iso, utc_now

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_catalog_overrides (
  station_uuid TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radio_catalog_deletions (
  station_uuid TEXT PRIMARY KEY,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_stations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  station_uuid TEXT NOT NULL UNIQUE,
  local_station_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  country_code TEXT,
  state TEXT,
  language_codes_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  favicon_url TEXT,
  homepage_url TEXT,
  codec TEXT,
  bitrate_kbps INTEGER,
  is_hls INTEGER NOT NULL DEFAULT 0,
  stream_url_resolved TEXT,
  desired_state TEXT NOT NULL DEFAULT 'stopped'
    CHECK (desired_state IN ('active', 'stopped')),
  actual_state TEXT NOT NULL DEFAULT 'stopped'
    CHECK (actual_state IN (
      'available', 'pending_probe', 'probing', 'pending_capacity', 'activating',
      'active', 'degraded', 'failed_probe', 'stopping', 'stopped')),
  probe_status TEXT,
  probe_checked_at_utc TEXT,
  last_error TEXT,
  active_campaign_count INTEGER NOT NULL DEFAULT 0,
  legacy_pinned INTEGER NOT NULL DEFAULT 0,
  stop_after_utc TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_probe_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  managed_station_id INTEGER NOT NULL REFERENCES managed_stations(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
  codec TEXT,
  sample_rate INTEGER,
  channels INTEGER,
  duration_seconds REAL,
  redirect_chain_json TEXT NOT NULL DEFAULT '[]',
  final_hostname TEXT,
  error TEXT,
  checked_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  managed_station_id INTEGER NOT NULL REFERENCES managed_stations(id) ON DELETE CASCADE,
  action TEXT NOT NULL CHECK (action IN ('probe', 'activate', 'stop', 'reprobe')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_station_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('explicit', 'country_top', 'country_all')),
  country_codes_json TEXT NOT NULL DEFAULT '[]',
  maximum_stations INTEGER NOT NULL DEFAULT 5,
  filters_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_station_members (
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  managed_station_id INTEGER NOT NULL REFERENCES managed_stations(id) ON DELETE CASCADE,
  PRIMARY KEY (campaign_id, managed_station_id)
);

CREATE TABLE IF NOT EXISTS capacity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preview_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  station_uuid TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_managed_stations_state ON managed_stations(actual_state);
CREATE INDEX IF NOT EXISTS idx_station_jobs_status ON station_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_probe_results_station
  ON station_probe_results(managed_station_id, checked_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_rules_campaign ON campaign_station_rules(campaign_id);
"""

ACTIVE_LIKE_STATES = ("activating", "active", "degraded", "stopping")


def local_station_id_for(station_uuid: str) -> str:
    return f"rb-{station_uuid.strip().lower()}"


class CatalogStore:
    """v0.4 persistence layered over the existing Database connection."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- migration ---------------------------------------------------------

    def migrate(self) -> None:
        with self._db.transaction() as connection:
            connection.executescript(CATALOG_SCHEMA)

    # -- curated overlay ---------------------------------------------------

    def load_overlay(
        self,
        overrides: list[dict[str, Any]],
        deleted_station_uuids: list[str],
    ) -> None:
        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute("DELETE FROM radio_catalog_overrides")
            connection.execute("DELETE FROM radio_catalog_deletions")
            for record in overrides:
                connection.execute(
                    "INSERT INTO radio_catalog_overrides(station_uuid, payload_json, updated_at)"
                    " VALUES (?, ?, ?)",
                    (str(record["station_uuid"]).lower(), json.dumps(record, sort_keys=True), now),
                )
            for station_uuid in deleted_station_uuids:
                connection.execute(
                    "INSERT OR IGNORE INTO radio_catalog_deletions(station_uuid, updated_at)"
                    " VALUES (?, ?)",
                    (str(station_uuid).lower(), now),
                )

    def overrides_map(self) -> dict[str, dict[str, Any]]:
        rows = self._db.transaction_read(
            "SELECT station_uuid, payload_json FROM radio_catalog_overrides"
        )
        return {str(row["station_uuid"]): json.loads(str(row["payload_json"])) for row in rows}

    def deleted_uuids(self) -> set[str]:
        rows = self._db.transaction_read("SELECT station_uuid FROM radio_catalog_deletions")
        return {str(row["station_uuid"]) for row in rows}

    # -- managed stations ---------------------------------------------------

    def upsert_managed_station(self, station: dict[str, Any]) -> int:
        """Insert or refresh a managed station keyed by station_uuid; returns id."""
        now = iso(utc_now())
        station_uuid = str(station["station_uuid"]).lower()
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM managed_stations WHERE station_uuid = ?", (station_uuid,)
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO managed_stations(
                      station_uuid, local_station_id, name, country_code, state,
                      language_codes_json, tags_json, favicon_url, homepage_url,
                      codec, bitrate_kbps, is_hls, stream_url_resolved,
                      desired_state, actual_state, legacy_pinned, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        station_uuid,
                        station.get("local_station_id") or local_station_id_for(station_uuid),
                        str(station.get("name") or station_uuid),
                        station.get("country_code"),
                        station.get("state"),
                        json.dumps(station.get("language_codes") or []),
                        json.dumps(station.get("tags") or []),
                        station.get("favicon_url"),
                        station.get("homepage_url"),
                        station.get("codec"),
                        station.get("bitrate_kbps"),
                        1 if station.get("is_hls") else 0,
                        station.get("stream_url_resolved"),
                        str(station.get("desired_state") or "stopped"),
                        str(station.get("actual_state") or "available"),
                        1 if station.get("legacy_pinned") else 0,
                        now,
                        now,
                    ),
                )
                return int(cursor.lastrowid or 0)
            connection.execute(
                """
                UPDATE managed_stations SET
                  name = ?, country_code = ?, state = ?, language_codes_json = ?,
                  tags_json = ?, favicon_url = ?, homepage_url = ?, codec = ?,
                  bitrate_kbps = ?, is_hls = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(station.get("name") or station_uuid),
                    station.get("country_code"),
                    station.get("state"),
                    json.dumps(station.get("language_codes") or []),
                    json.dumps(station.get("tags") or []),
                    station.get("favicon_url"),
                    station.get("homepage_url"),
                    station.get("codec"),
                    station.get("bitrate_kbps"),
                    1 if station.get("is_hls") else 0,
                    now,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])

    def import_legacy_station(
        self,
        *,
        local_station_id: str,
        name: str,
        country_code: str | None,
        language_codes: list[str],
        station_uuid: str | None = None,
    ) -> int:
        """Register an already-running pipeline station (e.g. hertz879) as an
        active, pinned managed station without restarting anything."""
        marker = station_uuid or f"legacy-{local_station_id}"
        now = iso(utc_now())
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM managed_stations WHERE local_station_id = ?",
                (local_station_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO managed_stations(
                  station_uuid, local_station_id, name, country_code, state,
                  language_codes_json, tags_json, desired_state, actual_state,
                  probe_status, legacy_pinned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, '[]', 'active', 'active', 'ok', 1, ?, ?)
                """,
                (marker.lower(), local_station_id, name, country_code,
                 json.dumps(language_codes), now, now),
            )
            return int(cursor.lastrowid or 0)

    def managed_station(self, managed_station_id: int) -> dict[str, Any] | None:
        rows = self._db.transaction_read(
            "SELECT * FROM managed_stations WHERE id = ?", (managed_station_id,)
        )
        return self._station_row(rows[0]) if rows else None

    def managed_station_by_uuid(self, station_uuid: str) -> dict[str, Any] | None:
        rows = self._db.transaction_read(
            "SELECT * FROM managed_stations WHERE station_uuid = ?",
            (station_uuid.strip().lower(),),
        )
        return self._station_row(rows[0]) if rows else None

    def list_managed_stations(self) -> list[dict[str, Any]]:
        rows = self._db.transaction_read(
            "SELECT * FROM managed_stations ORDER BY created_at"
        )
        return [self._station_row(row) for row in rows]

    def set_station_state(
        self,
        managed_station_id: int,
        *,
        actual_state: str | None = None,
        desired_state: str | None = None,
        probe_status: str | None = None,
        last_error: str | None = ...,  # type: ignore[assignment]
        stream_url_resolved: str | None = ...,  # type: ignore[assignment]
        stop_after_utc: datetime | None = ...,  # type: ignore[assignment]
    ) -> None:
        sets: list[str] = ["updated_at = ?"]
        args: list[Any] = [iso(utc_now())]
        if actual_state is not None:
            sets.append("actual_state = ?")
            args.append(actual_state)
        if desired_state is not None:
            sets.append("desired_state = ?")
            args.append(desired_state)
        if probe_status is not None:
            sets.append("probe_status = ?")
            args.append(probe_status)
            sets.append("probe_checked_at_utc = ?")
            args.append(iso(utc_now()))
        if last_error is not ...:
            sets.append("last_error = ?")
            args.append(last_error)
        if stream_url_resolved is not ...:
            sets.append("stream_url_resolved = ?")
            args.append(stream_url_resolved)
        if stop_after_utc is not ...:
            sets.append("stop_after_utc = ?")
            args.append(iso(stop_after_utc) if stop_after_utc else None)
        args.append(managed_station_id)
        with self._db.transaction() as connection:
            connection.execute(
                f"UPDATE managed_stations SET {', '.join(sets)} WHERE id = ?", args
            )

    def stream_url_for(self, managed_station_id: int) -> str | None:
        """Backend-only accessor used by the reconciler; never expose via API."""
        rows = self._db.transaction_read(
            "SELECT stream_url_resolved FROM managed_stations WHERE id = ?",
            (managed_station_id,),
        )
        if not rows:
            return None
        value = rows[0]["stream_url_resolved"]
        return str(value) if value else None

    # -- probe results -------------------------------------------------------

    def record_probe_result(self, managed_station_id: int, result: dict[str, Any]) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO station_probe_results(
                  managed_station_id, status, codec, sample_rate, channels,
                  duration_seconds, redirect_chain_json, final_hostname, error,
                  checked_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    managed_station_id,
                    str(result.get("status") or "failed"),
                    result.get("codec"),
                    result.get("sample_rate"),
                    result.get("channels"),
                    result.get("duration_seconds"),
                    json.dumps(result.get("redirect_chain") or []),
                    result.get("final_hostname"),
                    result.get("error"),
                    iso(utc_now()),
                ),
            )

    def latest_probe_result(self, managed_station_id: int) -> dict[str, Any] | None:
        rows = self._db.transaction_read(
            "SELECT * FROM station_probe_results WHERE managed_station_id = ?"
            " ORDER BY checked_at_utc DESC LIMIT 1",
            (managed_station_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "managed_station_id": int(row["managed_station_id"]),
            "status": str(row["status"]),
            "codec": row["codec"],
            "sample_rate": row["sample_rate"],
            "channels": row["channels"],
            "duration_seconds": row["duration_seconds"],
            "redirect_chain": json.loads(str(row["redirect_chain_json"])),
            "final_hostname": row["final_hostname"],
            "error": row["error"],
            "checked_at_utc": str(row["checked_at_utc"]),
        }

    # -- station jobs ---------------------------------------------------------

    def enqueue_job(self, managed_station_id: int, action: str) -> int:
        """Queue a reconciler job; duplicate pending jobs collapse to one."""
        now = iso(utc_now())
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM station_jobs WHERE managed_station_id = ? AND action = ?"
                " AND status IN ('pending', 'running')",
                (managed_station_id, action),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                "INSERT INTO station_jobs(managed_station_id, action, status, created_at, updated_at)"
                " VALUES (?, ?, 'pending', ?, ?)",
                (managed_station_id, action, now, now),
            )
            return int(cursor.lastrowid or 0)

    def claim_next_job(self) -> dict[str, Any] | None:
        """Claim exactly one pending job; only one job may run at a time."""
        now = iso(utc_now())
        with self._db.transaction() as connection:
            running = connection.execute(
                "SELECT count(*) AS n FROM station_jobs WHERE status = 'running'"
            ).fetchone()
            if int(running["n"]) > 0:
                return None
            row = connection.execute(
                "SELECT * FROM station_jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE station_jobs SET status = 'running', attempts = attempts + 1,"
                " updated_at = ? WHERE id = ?",
                (now, int(row["id"])),
            )
            return {
                "id": int(row["id"]),
                "managed_station_id": int(row["managed_station_id"]),
                "action": str(row["action"]),
                "attempts": int(row["attempts"]) + 1,
            }

    def finish_job(self, job_id: int, *, status: str, error: str | None = None) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE station_jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error, iso(utc_now()), job_id),
            )

    def job_counts(self) -> dict[str, int]:
        rows = self._db.transaction_read(
            "SELECT status, count(*) AS n FROM station_jobs GROUP BY status"
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    def oldest_running_job_age_seconds(self) -> int | None:
        rows = self._db.transaction_read(
            "SELECT created_at FROM station_jobs WHERE status IN ('pending', 'running')"
            " ORDER BY created_at LIMIT 1"
        )
        if not rows:
            return None
        started = datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z", "+00:00"))
        return max(0, int((utc_now() - started).total_seconds()))

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.transaction_read(
            "SELECT * FROM station_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [
            {
                "id": int(row["id"]),
                "managed_station_id": int(row["managed_station_id"]),
                "action": str(row["action"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    # -- campaign selection rules and members ---------------------------------

    def add_campaign_rule(
        self,
        campaign_id: str,
        *,
        mode: str,
        country_codes: list[str],
        maximum_stations: int,
        filters: dict[str, Any],
    ) -> int:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO campaign_station_rules(campaign_id, mode, country_codes_json,"
                " maximum_stations, filters_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    mode,
                    json.dumps(country_codes),
                    maximum_stations,
                    json.dumps(filters, sort_keys=True),
                    iso(utc_now()),
                ),
            )
            return int(cursor.lastrowid or 0)

    def rules_for_campaign(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self._db.transaction_read(
            "SELECT * FROM campaign_station_rules WHERE campaign_id = ? ORDER BY id",
            (campaign_id,),
        )
        return [
            {
                "id": int(row["id"]),
                "campaign_id": str(row["campaign_id"]),
                "mode": str(row["mode"]),
                "country_codes": json.loads(str(row["country_codes_json"])),
                "maximum_stations": int(row["maximum_stations"]),
                "filters": json.loads(str(row["filters_json"])),
            }
            for row in rows
        ]

    def set_campaign_members(self, campaign_id: str, managed_station_ids: list[int]) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM campaign_station_members WHERE campaign_id = ?", (campaign_id,)
            )
            for managed_station_id in managed_station_ids:
                connection.execute(
                    "INSERT OR IGNORE INTO campaign_station_members(campaign_id, managed_station_id)"
                    " VALUES (?, ?)",
                    (campaign_id, managed_station_id),
                )

    def members_for_campaign(self, campaign_id: str) -> list[int]:
        rows = self._db.transaction_read(
            "SELECT managed_station_id FROM campaign_station_members WHERE campaign_id = ?"
            " ORDER BY managed_station_id",
            (campaign_id,),
        )
        return [int(row["managed_station_id"]) for row in rows]

    def recompute_reference_counts(self, *, stop_grace_seconds: int) -> list[int]:
        """Recount active-campaign references per managed station.

        Returns managed station ids whose count dropped to zero right now (the
        caller schedules their stop after the grace period). Legacy pinned
        stations never receive a stop_after time.
        """
        due_zero: list[int] = []
        now = utc_now()
        with self._db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT ms.id, ms.active_campaign_count, ms.legacy_pinned, ms.stop_after_utc,
                  ms.desired_state, ms.actual_state,
                  (SELECT count(*) FROM campaign_station_members m
                     JOIN campaigns c ON c.id = m.campaign_id
                    WHERE m.managed_station_id = ms.id AND c.status = 'active') AS refs
                FROM managed_stations ms
                """
            ).fetchall()
            for row in rows:
                refs = int(row["refs"])
                station_id = int(row["id"])
                previous = int(row["active_campaign_count"])
                pinned = bool(row["legacy_pinned"])
                if refs != previous:
                    connection.execute(
                        "UPDATE managed_stations SET active_campaign_count = ?, updated_at = ?"
                        " WHERE id = ?",
                        (refs, iso(now), station_id),
                    )
                if refs == 0 and not pinned:
                    # Arm the stop timer on the 1 -> 0 transition AND for any
                    # station that still wants to run unreferenced: activation
                    # clears stop_after_utc, so a station promoted after its
                    # campaigns vanished must be re-armed here or it runs forever.
                    if row["stop_after_utc"] is None and (
                        previous > 0 or str(row["desired_state"]) == "active"
                    ):
                        connection.execute(
                            "UPDATE managed_stations SET stop_after_utc = ?, updated_at = ?"
                            " WHERE id = ?",
                            (iso(now + timedelta(seconds=stop_grace_seconds)), iso(now), station_id),
                        )
                        due_zero.append(station_id)
                elif refs > 0:
                    if row["stop_after_utc"] is not None:
                        connection.execute(
                            "UPDATE managed_stations SET stop_after_utc = NULL, updated_at = ?"
                            " WHERE id = ?",
                            (iso(now), station_id),
                        )
                    if str(row["actual_state"]) == "stopped" and not pinned:
                        # A referenced station that was wound down (e.g. its
                        # campaign was paused past the grace period) must come
                        # back: park it for the promotion pass to restart.
                        connection.execute(
                            "UPDATE managed_stations SET desired_state = 'active',"
                            " actual_state = 'pending_capacity', updated_at = ?"
                            " WHERE id = ?",
                            (iso(now), station_id),
                        )
        return due_zero

    def stations_due_for_stop(self) -> list[dict[str, Any]]:
        rows = self._db.transaction_read(
            "SELECT * FROM managed_stations WHERE stop_after_utc IS NOT NULL"
            " AND stop_after_utc <= ? AND legacy_pinned = 0"
            " AND actual_state IN"
            " ('active', 'degraded', 'activating', 'pending_capacity', 'pending_probe')",
            (iso(utc_now()),),
        )
        return [self._station_row(row) for row in rows]

    def active_station_count(self) -> int:
        rows = self._db.transaction_read(
            "SELECT count(*) AS n FROM managed_stations WHERE actual_state IN"
            " ('activating', 'active', 'degraded', 'stopping')"
        )
        return int(rows[0]["n"]) if rows else 0

    def state_counts(self) -> dict[str, int]:
        rows = self._db.transaction_read(
            "SELECT actual_state, count(*) AS n FROM managed_stations GROUP BY actual_state"
        )
        return {str(row["actual_state"]): int(row["n"]) for row in rows}

    def add_campaign_station_ids(self, campaign_id: str, local_station_ids: list[str]) -> None:
        """Bridge v0.4 managed stations into the legacy campaign_stations table
        so the sync engine attributes mentions to rb-<uuid> pipeline stations."""
        with self._db.transaction() as connection:
            for station_id in local_station_ids:
                connection.execute(
                    "INSERT OR IGNORE INTO campaign_stations(campaign_id, station_id)"
                    " VALUES (?, ?)",
                    (campaign_id, station_id),
                )

    def bump_campaign_revision(self) -> None:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key='campaign_revision'"
            ).fetchone()
            current = int(row["value"]) if row is not None else 0
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES('campaign_revision', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current + 1),),
            )

    # -- audit -----------------------------------------------------------------

    def record_capacity_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO capacity_snapshots(snapshot_json, created_at) VALUES (?, ?)",
                (json.dumps(snapshot, sort_keys=True, default=str), iso(utc_now())),
            )

    def record_preview_audit(self, station_uuid: str, action: str, detail: str | None) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO preview_audit(station_uuid, action, detail, created_at)"
                " VALUES (?, ?, ?, ?)",
                (station_uuid.lower(), action, detail, iso(utc_now())),
            )

    # -- helpers -----------------------------------------------------------------

    @staticmethod
    def _station_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "station_uuid": str(row["station_uuid"]),
            "local_station_id": str(row["local_station_id"]),
            "name": str(row["name"]),
            "country_code": row["country_code"],
            "state": row["state"],
            "language_codes": json.loads(str(row["language_codes_json"])),
            "tags": json.loads(str(row["tags_json"])),
            "favicon_url": row["favicon_url"],
            "homepage_url": row["homepage_url"],
            "codec": row["codec"],
            "bitrate_kbps": row["bitrate_kbps"],
            "is_hls": bool(row["is_hls"]),
            "desired_state": str(row["desired_state"]),
            "actual_state": str(row["actual_state"]),
            "probe_status": row["probe_status"],
            "probe_checked_at_utc": row["probe_checked_at_utc"],
            "last_error": row["last_error"],
            "active_campaign_count": int(row["active_campaign_count"]),
            "legacy_pinned": bool(row["legacy_pinned"]),
            "stop_after_utc": row["stop_after_utc"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
