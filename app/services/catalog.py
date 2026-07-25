"""Catalogue service: Radio Browser search merged with the curated overlay.

Merge rules:
- Deleted UUIDs (curated deletion list) are hidden from every catalogue
  response and blocked from activation.
- Override records replace matching catalogue fields by station UUID.
- Overlay-only stations (not returned by Radio Browser for a query) may appear
  in country searches, but they still pass the EC2 probe before activation.
- Public responses never contain raw or resolved stream URLs.
"""
from __future__ import annotations

import time
from typing import Any

from ..db_catalog import CatalogStore
from .radio_browser import RadioBrowserClient


def _normalize_rb_station(raw: dict[str, Any]) -> dict[str, Any]:
    language_codes = [
        code.strip().lower()
        for code in str(raw.get("languagecodes") or "").split(",")
        if code.strip()
    ]
    tags = [tag.strip() for tag in str(raw.get("tags") or "").split(",") if tag.strip()]
    return {
        "station_uuid": str(raw.get("stationuuid") or "").lower(),
        "name": str(raw.get("name") or "").strip(),
        "country_code": (str(raw.get("countrycode") or "").upper() or None),
        "country_name": (str(raw.get("country") or "") or None),
        "state": (str(raw.get("state") or "") or None),
        "iso_3166_2": (str(raw.get("iso_3166_2") or "") or None),
        "language_codes": language_codes,
        "tags": tags,
        "favicon_url": (str(raw.get("favicon") or "") or None),
        "homepage_url": (str(raw.get("homepage") or "") or None),
        "codec": (str(raw.get("codec") or "") or None),
        "bitrate_kbps": int(raw.get("bitrate") or 0) or None,
        "is_hls": bool(raw.get("hls")),
        "radio_browser_healthy": bool(raw.get("lastcheckok")),
        "votes": int(raw.get("votes") or 0),
        "click_count": int(raw.get("clickcount") or 0),
        "source": "radio-browser",
    }


def _overlay_station(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "station_uuid": str(record.get("station_uuid") or "").lower(),
        "name": str(record.get("name") or "").strip(),
        "country_code": (str(record.get("country_code") or "").upper() or None),
        "country_name": None,
        "state": None,
        "iso_3166_2": record.get("iso_3166_2") or None,
        "language_codes": list(record.get("language_codes") or []),
        "tags": list(record.get("tags") or []),
        "favicon_url": record.get("favicon") or None,
        "homepage_url": record.get("homepage") or None,
        "codec": None,
        "bitrate_kbps": None,
        "is_hls": False,
        "radio_browser_healthy": None,
        "votes": 0,
        "click_count": 0,
        "source": "curated-overlay",
    }


class CatalogService:
    def __init__(self, client: RadioBrowserClient, store: CatalogStore) -> None:
        self._client = client
        self._store = store
        self._overlay_cache: tuple[float, dict[str, dict[str, Any]], set[str]] | None = None

    # -- overlay ------------------------------------------------------------

    def _overlay(self) -> tuple[dict[str, dict[str, Any]], set[str]]:
        now = time.monotonic()
        if self._overlay_cache is not None and now - self._overlay_cache[0] < 300:
            return self._overlay_cache[1], self._overlay_cache[2]
        overrides = self._store.overrides_map()
        deletions = self._store.deleted_uuids()
        self._overlay_cache = (now, overrides, deletions)
        return overrides, deletions

    def is_deleted(self, station_uuid: str) -> bool:
        _, deletions = self._overlay()
        return station_uuid.strip().lower() in deletions

    # -- reference lists ------------------------------------------------------

    def countries(self) -> list[dict[str, Any]]:
        items = []
        for raw in self._client.countries():
            code = str(raw.get("iso_3166_1") or raw.get("code") or "").upper()
            name = str(raw.get("name") or "").strip()
            if len(code) == 2 and name:
                items.append(
                    {"code": code, "name": name, "station_count": int(raw.get("stationcount") or 0)}
                )
        items.sort(key=lambda item: (-item["station_count"], item["code"]))
        return items

    def languages(self) -> list[dict[str, Any]]:
        items = []
        for raw in self._client.languages():
            name = str(raw.get("name") or "").strip()
            code = str(raw.get("iso_639") or "").strip().lower() or name.lower()
            if name:
                items.append(
                    {"code": code, "name": name, "station_count": int(raw.get("stationcount") or 0)}
                )
        items.sort(key=lambda item: -item["station_count"])
        return items

    def tags(self, *, limit: int = 200) -> list[dict[str, Any]]:
        items = [
            {"name": str(raw.get("name") or "").strip(),
             "station_count": int(raw.get("stationcount") or 0)}
            for raw in self._client.tags()
            if str(raw.get("name") or "").strip()
        ]
        items.sort(key=lambda item: -item["station_count"])
        return items[:limit]

    def codecs(self) -> list[dict[str, Any]]:
        items = [
            {"name": str(raw.get("name") or "").strip(),
             "station_count": int(raw.get("stationcount") or 0)}
            for raw in self._client.codecs()
            if str(raw.get("name") or "").strip()
        ]
        items.sort(key=lambda item: -item["station_count"])
        return items

    # -- station search --------------------------------------------------------

    def search_stations(
        self,
        *,
        country_code: str | None = None,
        query: str | None = None,
        state: str | None = None,
        language: str | None = None,
        tag: str | None = None,
        tag_list: list[str] | None = None,
        codec: str | None = None,
        bitrate_min: int | None = None,
        bitrate_max: int | None = None,
        https_only: bool = False,
        healthy_only: bool = True,
        offset: int = 0,
        limit: int = 50,
        order: str = "votes",
        reverse: bool = True,
    ) -> dict[str, Any]:
        overrides, deletions = self._overlay()
        # Request one extra row so has_more is knowable after deletion filtering.
        raw_rows = self._client.search_stations(
            name=query or None,
            countrycode=country_code or None,
            state=state or None,
            language=language or None,
            tag=tag or None,
            tag_list=",".join(tag_list) if tag_list else None,
            codec=codec or None,
            bitrate_min=bitrate_min,
            bitrate_max=bitrate_max,
            is_https=True if https_only else None,
            hidebroken=healthy_only,
            order=order,
            reverse=reverse,
            offset=offset,
            limit=min(limit, 100) + 1,
        )
        items: list[dict[str, Any]] = []
        for raw in raw_rows:
            station = _normalize_rb_station(raw)
            uuid = station["station_uuid"]
            if not uuid or uuid in deletions:
                continue
            override = overrides.get(uuid)
            if override is not None:
                merged = _overlay_station(override)
                for key in ("country_name", "codec", "bitrate_kbps", "is_hls",
                            "radio_browser_healthy", "votes", "click_count", "state"):
                    merged[key] = station[key]
                if not merged["language_codes"]:
                    merged["language_codes"] = station["language_codes"]
                merged["source"] = "radio-browser+overlay"
                station = merged
            items.append(station)
        has_more = len(items) > limit or len(raw_rows) > limit
        items = items[:limit]
        self._attach_monitoring(items)
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "source": "radio-browser",
            "mirror": self._client.last_mirror,
            "cache_age_seconds": 0,
        }

    def station_by_uuid(self, station_uuid: str) -> dict[str, Any] | None:
        uuid = station_uuid.strip().lower()
        overrides, deletions = self._overlay()
        if uuid in deletions:
            return None
        raw = self._client.station_by_uuid(uuid)
        station: dict[str, Any] | None = None
        if raw is not None:
            station = _normalize_rb_station(raw)
        override = overrides.get(uuid)
        if override is not None:
            merged = _overlay_station(override)
            if station is not None:
                for key in ("country_name", "codec", "bitrate_kbps", "is_hls",
                            "radio_browser_healthy", "votes", "click_count", "state"):
                    merged[key] = station[key]
                if not merged["language_codes"]:
                    merged["language_codes"] = station["language_codes"]
                merged["source"] = "radio-browser+overlay"
            station = merged
        if station is None:
            return None
        self._attach_monitoring([station])
        return station

    # -- monitoring join ----------------------------------------------------------

    def _attach_monitoring(self, stations: list[dict[str, Any]]) -> None:
        managed = {
            record["station_uuid"]: record for record in self._store.list_managed_stations()
        }
        for station in stations:
            record = managed.get(station["station_uuid"])
            if record is None:
                station["monitoring_status"] = "available"
                station["managed_station_id"] = None
                station["probe_status"] = None
                station["active_campaign_count"] = 0
            else:
                station["monitoring_status"] = record["actual_state"]
                station["managed_station_id"] = record["id"]
                station["probe_status"] = record["probe_status"]
                station["active_campaign_count"] = record["active_campaign_count"]
