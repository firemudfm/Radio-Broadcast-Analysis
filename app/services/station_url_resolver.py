"""Turn a subscribed station into a playable stream URL.

A campaign stores nothing but a station id. The listener needs a URL. Until this
module existed nothing bridged the two, so every subscription carried a NULL
``stream_url``, the listener skipped every station, and no audio was ever
captured -- the pipeline looked healthy and produced nothing.

Resolution order, cheapest first:

1. ``managed_stations.stream_url_resolved`` -- durable, no network. Once a
   station has been resolved it stays resolved across restarts and redeploys.
2. Radio Browser ``/json/url/<uuid>`` -- one call, and the answer is written
   back to (1) so it is never asked twice.

Step 2 is deliberately the fallback rather than the rule. Radio Browser counts
every ``/json/url`` call as a click for that station, so asking on a five-second
planner loop would both spam a free community service and distort its rankings.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from ..db_catalog import CatalogStore, local_station_id_for
from ..observability import log_fields

logger = logging.getLogger(__name__)


class SupportsResolveUrl(Protocol):
    def resolve_url(self, station_uuid: str) -> dict[str, Any]: ...


def station_uuid_from_local_id(station_id: str) -> str | None:
    """Inverse of :func:`local_station_id_for`.

    Returns ``None`` for an id this resolver cannot reason about, rather than
    guessing: a wrong uuid would resolve to another station's stream and
    attribute its mentions to this campaign.
    """
    candidate = str(station_id or "").strip().lower()
    if not candidate.startswith("rb-"):
        return None
    uuid = candidate[3:]
    return uuid or None


class StationUrlResolver:
    """Resolves and remembers the stream URL for a station id."""

    def __init__(
        self,
        database: Any,
        *,
        client: SupportsResolveUrl | None = None,
        store: CatalogStore | None = None,
    ) -> None:
        self._database = database
        self._client = client
        self._store = store if store is not None else CatalogStore(database)
        self._schema_ready = False

    def _ensure_schema(self) -> bool:
        """The catalogue tables are created by the API at start-up.

        The planner runs in its own container and may reach a station before
        the API has ever started, so it cannot assume the tables exist. Every
        statement in the catalogue schema is CREATE ... IF NOT EXISTS, so this
        is idempotent and safe to run concurrently with the API doing the same.
        """
        if self._schema_ready:
            return True
        try:
            self._store.migrate()
        except Exception:
            logger.exception("Station catalogue schema is unavailable")
            return False
        self._schema_ready = True
        return True

    def resolve(self, station_id: str) -> str | None:
        """The station's stream URL, or ``None`` with the reason logged."""
        if not self._ensure_schema():
            return None
        uuid = station_uuid_from_local_id(station_id)
        if uuid is None:
            logger.warning(
                "Station id is not a Radio Browser id; cannot resolve a stream URL",
                extra=log_fields(station_id=station_id),
            )
            return None

        record = self._store.managed_station_by_uuid(uuid)
        if record is not None:
            # Deliberately via stream_url_for: managed_station_by_uuid never
            # serialises stream_url_resolved, so that a stream URL cannot leak
            # into a public API response by accident.
            stored = str(self._store.stream_url_for(int(record["id"])) or "").strip()
            if stored:
                return stored

        if self._client is None:
            logger.warning(
                "No catalogue URL and no Radio Browser client available",
                extra=log_fields(station_id=station_id),
            )
            return None

        try:
            payload = self._client.resolve_url(uuid)
        except Exception:
            # Network, mirror outage, unknown uuid. Never fatal: one station
            # that cannot be resolved must not stop the planner reconciling
            # every other station.
            logger.warning(
                "Radio Browser could not resolve a stream URL",
                extra=log_fields(station_id=station_id),
                exc_info=True,
            )
            return None

        url = _url_from_payload(payload)
        if not url:
            logger.warning(
                "Radio Browser returned no usable stream URL",
                extra=log_fields(station_id=station_id),
            )
            return None

        self._remember(uuid, url, payload)
        logger.info(
            "Resolved a stream URL for a subscribed station",
            extra=log_fields(station_id=station_id),
        )
        return url

    def metadata(self, station_id: str) -> dict[str, Any]:
        """Catalogue fields worth copying onto the subscription, if known."""
        uuid = station_uuid_from_local_id(station_id)
        if uuid is None or not self._ensure_schema():
            return {}
        record = self._store.managed_station_by_uuid(uuid)
        if record is None:
            return {"station_uuid": uuid}
        languages = record.get("language_codes")
        return {
            "station_uuid": uuid,
            "display_name": str(record.get("name") or ""),
            "country_code": record.get("country_code"),
            # The subscription stores JSON; the catalogue row decodes it.
            "language_codes_json": json.dumps(languages) if languages else None,
        }

    def _remember(self, uuid: str, url: str, payload: dict[str, Any]) -> None:
        """Write the URL back so the next cycle needs no network at all."""
        record = self._store.managed_station_by_uuid(uuid)
        if record is None:
            # A campaign can reference a station that was never activated
            # through the monitoring API. Create the catalogue row rather than
            # refusing: the campaign is the operator saying they want it.
            managed_id = self._store.upsert_managed_station(
                {
                    "station_uuid": uuid,
                    "local_station_id": local_station_id_for(uuid),
                    "name": str(payload.get("name") or uuid).strip() or uuid,
                    "country_code": (str(payload.get("countrycode") or "").upper() or None),
                    "language_codes": _language_codes(payload),
                    "codec": (str(payload.get("codec") or "") or None),
                    "bitrate_kbps": _int_or_none(payload.get("bitrate")),
                    "is_hls": bool(payload.get("hls")),
                    "stream_url_resolved": url,
                }
            )
        else:
            managed_id = int(record["id"])
        self._store.set_station_state(managed_id, stream_url_resolved=url)


def _url_from_payload(payload: dict[str, Any]) -> str:
    """Prefer the resolved URL: ``url`` can still be a redirecting playlist."""
    for key in ("url_resolved", "url"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _language_codes(payload: dict[str, Any]) -> list[str]:
    raw = str(payload.get("languagecodes") or "")
    return [code.strip().lower() for code in raw.split(",") if code.strip()]


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


__all__ = ["StationUrlResolver", "station_uuid_from_local_id"]
