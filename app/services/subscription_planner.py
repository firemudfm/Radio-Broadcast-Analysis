"""Station subscription planner.

Turns campaign intent into station reality, de-duplicated at the station level:

* one subscription per **DISTINCT** station, whatever the campaign count;
* one combined keyword index per station, from every campaign referencing it;
* capacity admission in *unique active stations*, never campaigns or keywords;
* deterministic shard assignment so several listeners can divide stations with
  no coordination.

Reference-count transitions follow the semantics the v0.4 catalogue already
proved in production:

===========  ===============================================================
 0 -> 1      create the subscription
 1 -> N      reuse it; the reference count rises, nothing restarts
 N -> 1      keep it active
 1 -> 0      schedule wind-down after the grace period
===========  ===============================================================
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Settings
from ..observability import safe_extra
from ..pipeline.enums import ACTIVE_SUBSCRIPTION_STATES
from ..pipeline.ids import IdentifierError, stable_shard_index, validate_station_id
from .keyword_index import StationKeywordIndex, build_index

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class PlanResult:
    """What one planning cycle changed. Every field is an assertable number."""

    unique_requested: int
    unique_active: int
    pending_capacity: int
    created_subscriptions: int
    reused_subscriptions: int
    winding_down: int
    stopped: int
    index_versions_published: int
    reused_station_streams: int

    def as_dict(self) -> dict[str, int]:
        # Field names avoid the reserved LogRecord attributes (`created`,
        # `name`, `module`, `process`, ...): this mapping is passed straight to
        # `logging` as `extra=`, and a collision raises KeyError at runtime.
        return {
            "unique_requested_station_count": self.unique_requested,
            "unique_active_station_count": self.unique_active,
            "pending_capacity_station_count": self.pending_capacity,
            "created_subscriptions": self.created_subscriptions,
            "reused_subscriptions": self.reused_subscriptions,
            "winding_down": self.winding_down,
            "stopped": self.stopped,
            "index_versions_published": self.index_versions_published,
            "reused_station_stream_count": self.reused_station_streams,
        }


class SubscriptionPlanner:
    """Reconciles desired station state from active campaigns."""

    def __init__(
        self,
        settings: Settings,
        database: Any,
        *,
        clock=None,
        url_resolver=None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))
        # Optional so every existing caller and test keeps working. Without one
        # the planner behaves exactly as before: subscriptions are created, and
        # a station with no catalogue URL simply stays unresolved.
        self._url_resolver = url_resolver

    # -- desired state ---------------------------------------------------------

    def desired_bindings(self) -> dict[str, list[dict[str, Any]]]:
        """``{station_id: [keyword binding, ...]}`` for active campaigns.

        Reads the *existing* campaign tables, so the planner needs no parallel
        write path and campaigns created through the current API are picked up
        unchanged.
        """
        rows = self._database.read_all(
            """
            SELECT cs.station_id       AS station_id,
                   c.id                AS campaign_id,
                   k.id                AS keyword_id,
                   k.entity_id         AS entity_id,
                   k.value             AS canonical_value,
                   k.aliases_json      AS aliases_json,
                   k.match_mode        AS match_mode,
                   k.keyword_type      AS keyword_type,
                   k.semantic_matching AS semantic_matching,
                   k.semantic_threshold AS semantic_threshold,
                   p.policy_json       AS policy_json
            FROM campaigns c
            JOIN campaign_stations cs ON cs.campaign_id = c.id
            JOIN campaign_keywords k  ON k.campaign_id = c.id
            LEFT JOIN campaign_content_policies p ON p.campaign_id = c.id
            WHERE c.status = 'active' AND k.enabled = 1
            ORDER BY cs.station_id, k.value
            """
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            raw_station = str(row["station_id"])
            try:
                station_id = validate_station_id(raw_station)
            except IdentifierError:
                # A station id that cannot be used as a path segment or a
                # MessageGroupId is skipped loudly rather than sanitised: a
                # silently rewritten id would attribute mentions to the wrong
                # station.
                logger.warning(
                    "Skipping campaign station with an unsafe id", extra={"station_id": raw_station}
                )
                continue
            try:
                aliases = json.loads(str(row["aliases_json"] or "[]"))
            except (TypeError, ValueError):
                aliases = []
            policy = {}
            if row["policy_json"]:
                try:
                    policy = json.loads(str(row["policy_json"]))
                except (TypeError, ValueError):
                    policy = {}
            grouped.setdefault(station_id, []).append(
                {
                    "keyword_id": str(row["keyword_id"]),
                    "campaign_id": str(row["campaign_id"]),
                    "entity_id": str(row["entity_id"]),
                    "canonical_value": str(row["canonical_value"]),
                    "aliases": aliases,
                    "match_mode": str(row["match_mode"] or "tokens"),
                    "keyword_type": str(row["keyword_type"] or "brand"),
                    "semantic_matching": bool(row["semantic_matching"]),
                    "semantic_threshold": float(row["semantic_threshold"] or 0.74),
                    "content_policy": {**self._settings.content_policy_defaults, **policy},
                    "languages": [],
                }
            )
        return grouped

    # -- planning --------------------------------------------------------------

    def plan_once(self) -> PlanResult:
        """One full reconciliation cycle."""
        now = self._clock()
        desired = self.desired_bindings()
        reference_counts = {
            station_id: len({binding["campaign_id"] for binding in bindings})
            for station_id, bindings in desired.items()
        }
        existing = self._existing_subscriptions()

        created = self._upsert_subscriptions(desired, reference_counts, existing, now)
        reused = sum(1 for count in reference_counts.values() if count > 1)
        winding_down, stopped = self._wind_down_unreferenced(set(desired), existing, now)
        published = self._publish_indexes(desired, now)
        admitted, pending = self._admit_capacity(now)

        result = PlanResult(
            unique_requested=len(desired),
            unique_active=admitted,
            pending_capacity=pending,
            created_subscriptions=created,
            reused_subscriptions=len(desired) - created,
            winding_down=winding_down,
            stopped=stopped,
            index_versions_published=published,
            reused_station_streams=reused,
        )
        logger.info("Planner cycle complete", extra=safe_extra(result.as_dict()))
        return result

    def _existing_subscriptions(self) -> dict[str, dict[str, Any]]:
        rows = self._database.read_all("SELECT * FROM station_subscriptions")
        return {str(row["station_id"]): dict(row) for row in rows}

    def _upsert_subscriptions(
        self,
        desired: dict[str, list[dict[str, Any]]],
        reference_counts: dict[str, int],
        existing: dict[str, dict[str, Any]],
        now: datetime,
    ) -> int:
        """Create or refresh one row per DISTINCT station. Returns new rows."""
        stamp = _iso(now)
        shard_count = self._settings.RADIO_LISTENER_SHARD_COUNT
        created = 0

        resolved_budget = self._settings.RADIO_STATION_URL_RESOLVE_PER_CYCLE

        for station_id in sorted(desired):
            shard = stable_shard_index(station_id, shard_count)
            reference_count = reference_counts[station_id]
            record = existing.get(station_id)
            if record is None:
                created += 1

                def insert(connection: sqlite3.Connection, sid=station_id, s=shard, rc=reference_count) -> None:
                    connection.execute(
                        """
                        INSERT INTO station_subscriptions(
                          station_id, reference_count, state, shard_index,
                          created_at_utc, updated_at_utc
                        ) VALUES (?, ?, 'desired', ?, ?, ?)
                        ON CONFLICT(station_id) DO UPDATE SET
                          reference_count=excluded.reference_count,
                          shard_index=excluded.shard_index,
                          updated_at_utc=excluded.updated_at_utc
                        """,
                        (sid, rc, s, stamp, stamp),
                    )

                self._database.write(insert)
                if resolved_budget > 0 and self._fill_stream_url(station_id, None, now):
                    resolved_budget -= 1
                continue

            # Reuse: the reference count rises, the stream is not restarted.
            previous_state = str(record["state"])
            revive = previous_state == "stopped"
            new_state = "desired" if revive else previous_state
            if previous_state == "winding_down":
                # References came back before the grace period elapsed.
                new_state = "active" if record["state_reason"] == "was_active" else "desired"

            def update(
                connection: sqlite3.Connection,
                sid=station_id,
                s=shard,
                rc=reference_count,
                st=new_state,
            ) -> None:
                connection.execute(
                    "UPDATE station_subscriptions SET reference_count=?, shard_index=?,"
                    " state=?, winddown_after_utc=NULL, updated_at_utc=? WHERE station_id=?",
                    (rc, s, st, stamp, sid),
                )

            self._database.write(update)

            # Backfill. Every subscription created before stream-URL resolution
            # existed has a NULL url, and reuse never touches it -- so without
            # this the stations already on the host would stay skipped forever.
            if resolved_budget > 0 and self._fill_stream_url(station_id, record, now):
                resolved_budget -= 1
        return created

    def _fill_stream_url(
        self,
        station_id: str,
        record: dict[str, Any] | None,
        now: datetime,
    ) -> bool:
        """Give a subscription its stream URL. True when a lookup was spent.

        Returns True for an *attempt*, not a success: a failed attempt has cost
        a Radio Browser call and must count against the per-cycle budget.
        """
        if self._url_resolver is None:
            return False
        if record is not None:
            if str(record.get("stream_url") or "").strip():
                return False
            retry_after = _parse(record.get("stream_url_retry_after_utc"))
            if retry_after is not None and retry_after > now:
                # Backing off from an earlier failure.
                return False

        url = self._url_resolver.resolve(station_id)
        stamp = _iso(now)
        if not url:
            backoff = self._settings.RADIO_STATION_URL_RETRY_SECONDS
            retry_at = _iso(now + timedelta(seconds=backoff))

            def mark(connection: sqlite3.Connection, sid=station_id, at=retry_at, s=stamp) -> None:
                connection.execute(
                    "UPDATE station_subscriptions SET stream_url_retry_after_utc=?,"
                    " last_error='stream URL could not be resolved', updated_at_utc=?"
                    " WHERE station_id=?",
                    (at, s, sid),
                )

            self._database.write(mark)
            return True

        metadata = self._url_resolver.metadata(station_id)
        name = str(metadata.get("display_name") or "")
        station_uuid = metadata.get("station_uuid")
        country = metadata.get("country_code")
        languages = metadata.get("language_codes_json")

        def store(
            connection: sqlite3.Connection,
            sid=station_id,
            u=url,
            uuid=station_uuid,
            nm=name,
            cc=country,
            langs=languages,
            s=stamp,
        ) -> None:
            connection.execute(
                "UPDATE station_subscriptions SET stream_url=?, station_uuid=?,"
                " display_name=CASE WHEN ?='' THEN display_name ELSE ? END,"
                " country_code=COALESCE(?, country_code),"
                " language_codes_json=COALESCE(?, language_codes_json),"
                " stream_url_retry_after_utc=NULL, last_error=NULL, updated_at_utc=?"
                " WHERE station_id=?",
                (u, uuid, nm, nm, cc, langs, s, sid),
            )

        self._database.write(store)
        return True

    def _wind_down_unreferenced(
        self,
        referenced: set[str],
        existing: dict[str, dict[str, Any]],
        now: datetime,
    ) -> tuple[int, int]:
        """Arm and honour the wind-down timer for unreferenced stations."""
        stamp = _iso(now)
        grace = self._settings.RADIO_STATION_WINDDOWN_GRACE_SECONDS
        armed = 0
        stopped = 0

        for station_id, record in existing.items():
            if station_id in referenced:
                continue
            state = str(record["state"])
            if state == "stopped":
                continue
            deadline = _parse(record["winddown_after_utc"])
            if deadline is None:
                # Remember whether it was actually running, so a revive during
                # the grace period restores the right state rather than a
                # phantom "active" for a station that never started.
                reason = "was_active" if state in ACTIVE_SUBSCRIPTION_STATES else "was_idle"

                def arm(connection: sqlite3.Connection, sid=station_id, r=reason) -> None:
                    connection.execute(
                        "UPDATE station_subscriptions SET state='winding_down', state_reason=?,"
                        " winddown_after_utc=?, updated_at_utc=? WHERE station_id=?",
                        (r, _iso(now + timedelta(seconds=grace)), stamp, sid),
                    )

                self._database.write(arm)
                armed += 1
            elif deadline <= now:

                def stop(connection: sqlite3.Connection, sid=station_id) -> None:
                    connection.execute(
                        "UPDATE station_subscriptions SET state='stopped', state_reason='unreferenced',"
                        " winddown_after_utc=NULL, reference_count=0, updated_at_utc=?"
                        " WHERE station_id=?",
                        (stamp, sid),
                    )

                self._database.write(stop)
                stopped += 1
        return armed, stopped

    def _publish_indexes(
        self, desired: dict[str, list[dict[str, Any]]], now: datetime
    ) -> int:
        """Publish a new index version only when effective content changed."""
        stamp = _iso(now)
        published = 0
        for station_id, bindings in sorted(desired.items()):
            latest = self._database.read_one(
                "SELECT version, fingerprint FROM station_keyword_index_versions"
                " WHERE station_id=? ORDER BY version DESC LIMIT 1",
                (station_id,),
            )
            previous_version = int(latest["version"]) if latest else 0
            previous_fingerprint = str(latest["fingerprint"]) if latest else None

            index = build_index(
                station_id,
                bindings,
                previous_version=previous_version,
                previous_fingerprint=previous_fingerprint,
            )
            self._replace_bindings(station_id, index, stamp)
            if index.fingerprint == previous_fingerprint:
                continue

            payload = json.dumps(index.to_payload(), ensure_ascii=False, sort_keys=True)

            def write(connection: sqlite3.Connection, idx=index, sid=station_id, body=payload) -> None:
                connection.execute(
                    """
                    INSERT INTO station_keyword_index_versions(
                      station_id, version, fingerprint, keyword_count, alias_count,
                      campaign_count, payload_json, published_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_id, version) DO UPDATE SET
                      fingerprint=excluded.fingerprint,
                      keyword_count=excluded.keyword_count,
                      alias_count=excluded.alias_count,
                      campaign_count=excluded.campaign_count,
                      payload_json=excluded.payload_json,
                      published_at_utc=excluded.published_at_utc
                    """,
                    (
                        sid,
                        idx.version,
                        idx.fingerprint,
                        idx.keyword_count,
                        idx.alias_count,
                        idx.campaign_count,
                        body,
                        stamp,
                    ),
                )
                connection.execute(
                    "UPDATE station_subscriptions SET keyword_index_version=?, updated_at_utc=?"
                    " WHERE station_id=?",
                    (idx.version, stamp, sid),
                )

            self._database.write(write)
            published += 1
            logger.info(
                "Published keyword index",
                extra={
                    "station_id": station_id,
                    "version": index.version,
                    "keywords": index.keyword_count,
                    "campaigns": index.campaign_count,
                },
            )
        return published

    def _replace_bindings(
        self, station_id: str, index: StationKeywordIndex, stamp: str
    ) -> None:
        """Refresh the flattened binding rows used by API attribution queries."""

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM station_keyword_bindings WHERE station_id=?", (station_id,)
            )
            for entry in index.entries:
                connection.execute(
                    """
                    INSERT INTO station_keyword_bindings(
                      station_id, keyword_id, campaign_id, entity_id, canonical_value,
                      keyword_type, match_mode, semantic_matching, semantic_threshold,
                      aliases_json, languages_json, content_policy_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        station_id,
                        entry.keyword_id,
                        entry.campaign_id,
                        entry.entity_id,
                        entry.canonical_value,
                        entry.keyword_type,
                        entry.match_mode,
                        int(entry.semantic_matching),
                        entry.semantic_threshold,
                        json.dumps(
                            [
                                {"value": a.value, "language": a.language, "kind": a.kind}
                                for a in entry.aliases
                            ],
                            ensure_ascii=False,
                        ),
                        json.dumps(list(entry.languages)),
                        json.dumps(entry.content_policy, sort_keys=True),
                        stamp,
                    ),
                )

        self._database.write(write)

    def _admit_capacity(self, now: datetime) -> tuple[int, int]:
        """Admit up to the capacity limit; park the rest in pending_capacity.

        Overflow is a first-class visible state, never a silent drop.
        """
        stamp = _iso(now)
        limit = self._settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS
        rows = self._database.read_all(
            "SELECT station_id, state, reference_count FROM station_subscriptions"
            " WHERE state NOT IN ('stopped') ORDER BY station_id"
        )
        already_active = [
            str(row["station_id"])
            for row in rows
            if str(row["state"]) in ACTIVE_SUBSCRIPTION_STATES
        ]
        candidates = [
            str(row["station_id"])
            for row in rows
            if str(row["state"]) in {"desired", "pending_capacity"}
            and int(row["reference_count"]) > 0
        ]
        free = max(0, limit - len(already_active))
        admitted = candidates[:free]
        deferred = candidates[free:]

        for station_id in admitted:
            self._database.write(
                lambda connection, sid=station_id: connection.execute(
                    "UPDATE station_subscriptions SET state='starting', state_reason=NULL,"
                    " updated_at_utc=? WHERE station_id=?",
                    (stamp, sid),
                )
            )
        for station_id in deferred:
            self._database.write(
                lambda connection, sid=station_id: connection.execute(
                    "UPDATE station_subscriptions SET state='pending_capacity',"
                    " state_reason=?, updated_at_utc=? WHERE station_id=?",
                    (
                        f"Active unique-station limit reached ({limit}); waiting for a slot",
                        stamp,
                        sid,
                    ),
                )
            )
        return len(already_active) + len(admitted), len(deferred)

    # -- reads for listeners and the API ---------------------------------------

    def assigned_stations(self, *, shard_index: int | None = None) -> list[dict[str, Any]]:
        """Stations this shard should be running."""
        index = (
            self._settings.RADIO_LISTENER_SHARD_INDEX if shard_index is None else shard_index
        )
        rows = self._database.read_all(
            "SELECT * FROM station_subscriptions WHERE shard_index=? AND state IN"
            " ('starting','active','degraded') ORDER BY station_id",
            (index,),
        )
        return [dict(row) for row in rows]

    def keyword_index_for(self, station_id: str) -> StationKeywordIndex | None:
        from .keyword_index import index_from_payload

        row = self._database.read_one(
            "SELECT payload_json FROM station_keyword_index_versions"
            " WHERE station_id=? ORDER BY version DESC LIMIT 1",
            (station_id,),
        )
        if row is None:
            return None
        try:
            return index_from_payload(json.loads(str(row["payload_json"])))
        except (TypeError, ValueError, KeyError):
            logger.exception("Stored keyword index is unreadable", extra={"station_id": station_id})
            return None

    def capacity_snapshot(self) -> dict[str, Any]:
        """Every counter the API exposes, each with a distinct meaning."""
        rows = self._database.read_all(
            "SELECT state, count(*) AS n FROM station_subscriptions GROUP BY state"
        )
        by_state = {str(row["state"]): int(row["n"]) for row in rows}
        unique_active = sum(by_state.get(state, 0) for state in ACTIVE_SUBSCRIPTION_STATES)

        reference_rows = self._database.read_all(
            "SELECT count(*) AS n FROM campaign_stations cs JOIN campaigns c"
            " ON c.id = cs.campaign_id WHERE c.status='active'"
        )
        requested = self._database.read_one(
            "SELECT count(*) AS n FROM station_subscriptions WHERE reference_count > 0"
        )
        shared = self._database.read_one(
            "SELECT count(*) AS n FROM station_subscriptions WHERE reference_count > 1"
        )

        return {
            "catalog_station_count": self._catalog_station_count(),
            "campaign_station_reference_count": int(reference_rows[0]["n"]) if reference_rows else 0,
            "unique_requested_station_count": int(requested["n"]) if requested else 0,
            "unique_active_station_count": unique_active,
            "pending_capacity_station_count": by_state.get("pending_capacity", 0),
            "reused_station_stream_count": int(shared["n"]) if shared else 0,
            "active_unique_station_limit": self._settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS,
            "listener_shard_count": self._settings.RADIO_LISTENER_SHARD_COUNT,
            "states": by_state,
        }

    def _catalog_station_count(self) -> int:
        """Catalogue size, or 0 when the v0.4 catalogue is not migrated here.

        `managed_stations` belongs to `CatalogStore`, which the API lifespan
        migrates. A planner or worker container that never constructs it must
        still be able to report capacity rather than crashing on a missing
        table it does not own.
        """
        present = self._database.read_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_stations'"
        )
        if present is None:
            return 0
        row = self._database.read_one("SELECT count(*) AS n FROM managed_stations")
        return int(row["n"]) if row else 0


__all__ = ["PlanResult", "SubscriptionPlanner"]
