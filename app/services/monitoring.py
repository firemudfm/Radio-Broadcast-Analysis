"""Managed-station monitoring: capacity, admission, estimates, activation.

The API layer only records intent; the planner turns that into shared station
subscriptions. Admission is capacity-aware against two DIFFERENT limits:

  * RADIO_MAX_REQUESTED_UNIQUE_STATIONS -- how many distinct stations campaigns
    may ask for. A control-plane number, proven to 1,000.
  * RADIO_MAX_ACTIVE_UNIQUE_STATIONS -- how many are decoded at once. A compute
    number, currently 1 on this host.

Selecting a whole country therefore produces a plan, not a thousand decodes:
stations beyond the active limit are parked as `pending_capacity` and picked up
as slots free. The API still reports the active limit as `active_station_limit`,
which is the field name the frontend already consumes.
"""
from __future__ import annotations

import os
from typing import Any

from ..config import Settings
from ..db_catalog import CatalogStore
from .catalog import CatalogService


class MonitoringError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    values[parts[0][:-1]] = int(parts[1])
    except OSError:
        pass
    return values


class MonitoringService:
    def __init__(
        self,
        settings: Settings,
        store: CatalogStore,
        catalog: CatalogService,
    ) -> None:
        self._settings = settings
        self._store = store
        self._catalog = catalog

    # -- capacity -----------------------------------------------------------

    def capacity(self) -> dict[str, Any]:
        active = self._store.active_station_count()
        limit = self._settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS
        requested_limit = self._settings.RADIO_MAX_REQUESTED_UNIQUE_STATIONS
        states = self._store.state_counts()
        pending_probe = states.get("pending_probe", 0) + states.get("probing", 0)
        pending_capacity = states.get("pending_capacity", 0)
        meminfo = _read_meminfo()
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        can_add = active < limit
        reason = (
            f"{active}/{limit} station slots in use"
            if can_add
            else (
                f"Active station limit reached ({active}/{limit}); further stations "
                f"are accepted and parked as pending_capacity. Raising "
                f"RADIO_MAX_ACTIVE_UNIQUE_STATIONS requires a live benchmark."
            )
        )
        snapshot = {
            "vcpus": os.cpu_count() or 1,
            "memory_total_gib": round(meminfo.get("MemTotal", 0) / 1024 / 1024, 2),
            "memory_available_gib": (
                round(meminfo["MemAvailable"] / 1024 / 1024, 2)
                if "MemAvailable" in meminfo
                else None
            ),
            "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2),
            "load_15m": round(load[2], 2),
            "active_stations": active,
            # Field name preserved for the frontend; it now sources the
            # shared-pipeline active limit.
            "active_station_limit": limit,
            "requested_station_limit": requested_limit,
            "pending_probe": pending_probe,
            "pending_capacity": pending_capacity,
            "oldest_processing_job_seconds": self._store.oldest_running_job_age_seconds(),
            "can_add_station": can_add,
            "reason": reason,
        }
        return snapshot

    # -- selection resolution ---------------------------------------------------

    def resolve_selection(self, selection: Any) -> tuple[list[dict[str, Any]], int]:
        """Resolve a StationSelection into catalogue stations.

        Returns (stations, matched_count). country_all never expands beyond the
        per-campaign maximum here; the remainder stays a rule + capacity plan.
        """
        mode = selection.mode
        per_campaign_cap = self._settings.RADIO_MAX_STATIONS_PER_CAMPAIGN
        if mode == "explicit":
            if len(selection.station_uuids) > per_campaign_cap:
                raise MonitoringError(
                    422,
                    f"Explicit selection exceeds RADIO_MAX_STATIONS_PER_CAMPAIGN={per_campaign_cap}",
                )
            stations: list[dict[str, Any]] = []
            for station_uuid in selection.station_uuids:
                if self._catalog.is_deleted(station_uuid):
                    raise MonitoringError(
                        422, f"Station {station_uuid} is removed by the curated deletion list"
                    )
                station = self._catalog.station_by_uuid(station_uuid)
                if station is None:
                    raise MonitoringError(404, f"Unknown station uuid: {station_uuid}")
                stations.append(station)
            return stations, len(stations)

        if mode == "country_all" and not self._settings.RADIO_ALLOW_COUNTRY_ALL:
            raise MonitoringError(
                403,
                "country_all selection is disabled on this instance (RADIO_ALLOW_COUNTRY_ALL=false);"
                " use country_top with a maximum station count",
            )

        filters = selection.filters
        matched = 0
        collected: list[dict[str, Any]] = []
        wanted = (
            min(selection.maximum_stations, per_campaign_cap)
            if mode == "country_top"
            else per_campaign_cap
        )
        for country_code in selection.country_codes:
            page = self._catalog.search_stations(
                country_code=country_code,
                language=filters.language,
                tag=filters.tags[0] if filters.tags else None,
                tag_list=filters.tags if len(filters.tags) > 1 else None,
                codec=filters.codec,
                bitrate_min=filters.bitrate_min,
                bitrate_max=filters.bitrate_max,
                https_only=filters.https_only,
                healthy_only=filters.healthy_only,
                offset=0,
                limit=100,
                order="votes",
                reverse=True,
            )
            matched += len(page["items"]) + (100 if page["has_more"] else 0)
            collected.extend(page["items"])
        collected.sort(key=lambda item: (-item["votes"], -item["click_count"]))
        return collected[:wanted], matched

    def estimate_selection(self, selection: Any) -> dict[str, Any]:
        stations, matched = self.resolve_selection(selection)
        capacity = self.capacity()
        already_active = 0
        pending_probe = 0
        failed = 0
        fresh = 0
        for station in stations:
            status = station["monitoring_status"]
            if status in {"active", "activating", "degraded"}:
                already_active += 1
            elif status in {"pending_probe", "probing"}:
                pending_probe += 1
            elif status == "failed_probe":
                failed += 1
            else:
                fresh += 1
        free_slots = max(0, capacity["active_station_limit"] - capacity["active_stations"])
        can_start_now = min(fresh, free_slots)
        pending_capacity = max(0, fresh - can_start_now)
        return {
            "mode": selection.mode,
            "matched_stations": matched,
            "selected_stations": len(stations),
            "already_active": already_active,
            "can_start_now": can_start_now,
            "pending_probe": pending_probe,
            "pending_capacity": pending_capacity,
            "failed": failed,
            "active_station_limit": capacity["active_station_limit"],
            "capacity_reason": capacity["reason"],
            "station_uuids_preview": [s["station_uuid"] for s in stations[:20]],
        }

    # -- activation lifecycle ---------------------------------------------------

    def request_probe(self, station_uuid: str) -> dict[str, Any]:
        station = self._require_station(station_uuid)
        managed_id = self._store.upsert_managed_station(station)
        record = self._store.managed_station(managed_id)
        assert record is not None
        if record["actual_state"] in {"active", "activating", "degraded"}:
            return {"managed_station_id": managed_id, "state": record["actual_state"], "job_id": None}
        self._store.set_station_state(managed_id, actual_state="pending_probe", last_error=None)
        job_id = self._store.enqueue_job(managed_id, "probe")
        return {"managed_station_id": managed_id, "state": "pending_probe", "job_id": job_id}

    def request_activation(self, station_uuid: str) -> dict[str, Any]:
        station = self._require_station(station_uuid)
        managed_id = self._store.upsert_managed_station(station)
        record = self._store.managed_station(managed_id)
        assert record is not None
        if record["actual_state"] in {"active", "activating"}:
            return {
                "managed_station_id": managed_id,
                "station_uuid": record["station_uuid"],
                "actual_state": record["actual_state"],
                "job_id": None,
                "detail": "Station is already active or activating",
            }
        capacity = self.capacity()
        if not capacity["can_add_station"]:
            self._store.set_station_state(
                managed_id, actual_state="pending_capacity", desired_state="active"
            )
            return {
                "managed_station_id": managed_id,
                "station_uuid": record["station_uuid"],
                "actual_state": "pending_capacity",
                "job_id": None,
                "detail": capacity["reason"] + self._unreferenced_note(record),
            }
        self._store.set_station_state(
            managed_id,
            actual_state="pending_probe",
            desired_state="active",
            last_error=None,
            stop_after_utc=None,
        )
        job_id = self._store.enqueue_job(managed_id, "activate")
        return {
            "managed_station_id": managed_id,
            "station_uuid": record["station_uuid"],
            "actual_state": "pending_probe",
            "job_id": job_id,
            "detail": "Activation queued: probe first, then pipeline start"
            + self._unreferenced_note(record),
        }

    @staticmethod
    def _unreferenced_note(record: dict[str, Any]) -> str:
        if int(record.get("active_campaign_count") or 0) > 0:
            return ""
        return (
            " Note: no active campaign references this station, so it stops"
            " automatically after the grace period unless a campaign uses it."
        )

    def request_stop(self, managed_station_id: int) -> dict[str, Any]:
        record = self._store.managed_station(managed_station_id)
        if record is None:
            raise MonitoringError(404, "Unknown managed station")
        if record["legacy_pinned"]:
            raise MonitoringError(409, "This station is pinned as legacy and cannot be stopped")
        if record["active_campaign_count"] > 0:
            raise MonitoringError(
                409,
                f"{record['active_campaign_count']} active campaign(s) still reference this station",
            )
        self._store.set_station_state(
            managed_station_id, desired_state="stopped", actual_state="stopping"
        )
        job_id = self._store.enqueue_job(managed_station_id, "stop")
        return {
            "managed_station_id": managed_station_id,
            "actual_state": "stopping",
            "detail": f"Stop queued (job {job_id})",
        }

    # -- campaign wiring ---------------------------------------------------------

    def attach_campaign_selection(self, campaign_id: str, selection: Any) -> None:
        """Persist rules + members and queue activations for a new campaign."""
        stations, _ = self.resolve_selection(selection)
        self._store.add_campaign_rule(
            campaign_id,
            mode=selection.mode,
            country_codes=list(selection.country_codes),
            maximum_stations=selection.maximum_stations,
            filters=selection.filters.model_dump(),
        )
        member_ids: list[int] = []
        for station in stations:
            managed_id = self._store.upsert_managed_station(station)
            member_ids.append(managed_id)
        self._store.set_campaign_members(campaign_id, member_ids)
        self._store.recompute_reference_counts(
            stop_grace_seconds=self._settings.RADIO_STATION_STOP_GRACE_SECONDS
        )
        capacity = self.capacity()
        free_slots = max(0, capacity["active_station_limit"] - capacity["active_stations"])
        for managed_id in member_ids:
            record = self._store.managed_station(managed_id)
            if record is None or record["actual_state"] in {
                "active", "activating", "pending_probe", "probing",
            }:
                continue
            if free_slots > 0:
                self._store.set_station_state(
                    managed_id, actual_state="pending_probe", desired_state="active"
                )
                self._store.enqueue_job(managed_id, "activate")
                free_slots -= 1
            else:
                self._store.set_station_state(
                    managed_id, actual_state="pending_capacity", desired_state="active"
                )

    def on_campaign_status_change(self) -> None:
        """Recount references after pause/resume/delete and schedule stops."""
        self._store.recompute_reference_counts(
            stop_grace_seconds=self._settings.RADIO_STATION_STOP_GRACE_SECONDS
        )
        for record in self._store.stations_due_for_stop():
            if record["actual_state"] in ("pending_capacity", "pending_probe"):
                # Nothing runs for these; a stop job would touch units that
                # were never enabled and could resurrect a phantom state.
                self._store.set_station_state(
                    record["id"],
                    desired_state="stopped",
                    actual_state="stopped",
                    stop_after_utc=None,
                )
                continue
            self._store.set_station_state(record["id"], desired_state="stopped")
            self._store.enqueue_job(record["id"], "stop")

    def campaign_selection_summary(self, campaign_id: str) -> dict[str, Any] | None:
        rules = self._store.rules_for_campaign(campaign_id)
        member_ids = self._store.members_for_campaign(campaign_id)
        if not rules and not member_ids:
            return None
        counts = {"active": 0, "pending_probe": 0, "pending_capacity": 0, "failed": 0}
        stations: list[dict[str, Any]] = []
        for managed_id in member_ids:
            record = self._store.managed_station(managed_id)
            if record is None:
                continue
            state = record["actual_state"]
            if state in {"active", "activating", "degraded"}:
                counts["active"] += 1
            elif state in {"pending_probe", "probing"}:
                counts["pending_probe"] += 1
            elif state == "pending_capacity":
                counts["pending_capacity"] += 1
            elif state == "failed_probe":
                counts["failed"] += 1
            stations.append(
                {
                    "station_uuid": record["station_uuid"],
                    "station_id": record["local_station_id"],
                    "name": record["name"],
                    "monitoring_status": state,
                    "probe_status": record["probe_status"],
                }
            )
        return {
            "mode": rules[0]["mode"] if rules else "explicit",
            "selected_station_count": len(member_ids),
            "active_count": counts["active"],
            "pending_probe_count": counts["pending_probe"],
            "pending_capacity_count": counts["pending_capacity"],
            "failed_count": counts["failed"],
            "stations": stations,
        }

    # -- helpers ---------------------------------------------------------------------

    def _require_station(self, station_uuid: str) -> dict[str, Any]:
        if self._catalog.is_deleted(station_uuid):
            raise MonitoringError(
                410, "Station is removed by the curated deletion list and cannot be activated"
            )
        station = self._catalog.station_by_uuid(station_uuid)
        if station is None:
            raise MonitoringError(404, f"Unknown station uuid: {station_uuid}")
        return station
