from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db_catalog import CatalogStore, local_station_id_for
from app.models_catalog import StationSelection
from app.services.catalog import CatalogService
from app.services.monitoring import MonitoringError, MonitoringService

HERTZ_UUID = "0e30b79d-3977-4bb0-9e83-a1914cd757d0"
MANGO_UUID = "78012206-1aa1-11e9-a80b-52543be04c81"
DELETED_UUID = "11111111-2222-4333-8444-555555555555"
PLAIN_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def rb_station(uuid: str, name: str, votes: int = 10, **extra: Any) -> dict[str, Any]:
    base = {
        "stationuuid": uuid,
        "name": name,
        "url": "http://stream.example/live",
        "url_resolved": "http://stream.example/live",
        "homepage": "https://example.com",
        "favicon": "https://example.com/icon.png",
        "tags": "rock,indie",
        "countrycode": "DE",
        "country": "Germany",
        "state": "NRW",
        "iso_3166_2": "DE-NW",
        "languagecodes": "de,en",
        "language": "german",
        "codec": "MP3",
        "bitrate": 128,
        "hls": 0,
        "lastcheckok": 1,
        "votes": votes,
        "clickcount": votes * 2,
    }
    base.update(extra)
    return base


class FakeRadioBrowser:
    """Catalogue-facing stand-in for RadioBrowserClient."""

    last_mirror = "test.mirror"

    def __init__(self) -> None:
        self.stations = [
            rb_station(HERTZ_UUID, "Hertz 87.9 (stale RB name)", votes=50),
            rb_station(PLAIN_UUID, "Plain FM", votes=40),
            rb_station(DELETED_UUID, "Deleted FM", votes=99),
        ]

    def countries(self) -> list[dict[str, Any]]:
        return [{"name": "Germany", "iso_3166_1": "DE", "stationcount": 3}]

    def languages(self) -> list[dict[str, Any]]:
        return [{"name": "german", "iso_639": "de", "stationcount": 3}]

    def tags(self) -> list[dict[str, Any]]:
        return [{"name": "rock", "stationcount": 2}]

    def codecs(self) -> list[dict[str, Any]]:
        return [{"name": "MP3", "stationcount": 3}]

    def search_stations(self, **_params: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in self.stations]

    def station_by_uuid(self, station_uuid: str) -> dict[str, Any] | None:
        for item in self.stations:
            if item["stationuuid"] == station_uuid:
                return dict(item)
        return None

    def resolve_url(self, station_uuid: str) -> dict[str, Any]:
        return {"url": "http://stream.example/live", "ok": True}


@pytest.fixture
def store(database) -> CatalogStore:
    catalog_store = CatalogStore(database)
    catalog_store.migrate()
    catalog_store.load_overlay(
        [
            {
                "station_uuid": HERTZ_UUID,
                "name": "Hertz 87.9 - Campusradio für Bielefeld | MP3 HQ",
                "url": "https://stream.radiohertz.de/hertz-hq.mp3",
                "homepage": "https://www.radiohertz.de/",
                "favicon": "",
                "country_code": "DE",
                "language_codes": ["de", "en"],
                "tags": ["campus"],
            }
        ],
        [DELETED_UUID],
    )
    return catalog_store


@pytest.fixture
def catalog(store) -> CatalogService:
    return CatalogService(FakeRadioBrowser(), store)  # type: ignore[arg-type]


@pytest.fixture
def monitoring(settings, store, catalog) -> MonitoringService:
    return MonitoringService(settings, store, catalog)


# -- catalogue merge -----------------------------------------------------------


def test_search_applies_override_and_deletion(catalog) -> None:
    page = catalog.search_stations(country_code="DE", limit=50)
    uuids = [item["station_uuid"] for item in page["items"]]
    assert DELETED_UUID not in uuids, "curated deletions must be hidden"
    hertz = next(item for item in page["items"] if item["station_uuid"] == HERTZ_UUID)
    assert hertz["name"].startswith("Hertz 87.9 - Campusradio")
    assert hertz["source"] == "radio-browser+overlay"
    # Radio Browser runtime facts survive the override merge.
    assert hertz["votes"] == 50
    assert hertz["radio_browser_healthy"] is True


def test_no_stream_url_in_public_payloads(catalog) -> None:
    page = catalog.search_stations(country_code="DE")
    blob = json.dumps(page)
    assert "stream.example" not in blob
    assert "url_resolved" not in blob
    detail = catalog.station_by_uuid(PLAIN_UUID)
    assert detail is not None
    assert "stream.example" not in json.dumps(detail)


def test_station_detail_deleted_returns_none(catalog) -> None:
    assert catalog.station_by_uuid(DELETED_UUID) is None


def test_countries_normalized(catalog) -> None:
    countries = catalog.countries()
    assert countries == [{"code": "DE", "name": "Germany", "station_count": 3}]


# -- monitoring / capacity ---------------------------------------------------------


def test_activation_respects_capacity_limit(monitoring, store, settings) -> None:
    assert settings.RADIO_MAX_ACTIVE_STATIONS == 2
    first = monitoring.request_activation(HERTZ_UUID)
    assert first["actual_state"] == "pending_probe"
    store.set_station_state(first["managed_station_id"], actual_state="active")
    second = monitoring.request_activation(PLAIN_UUID)
    store.set_station_state(second["managed_station_id"], actual_state="active")
    # Limit reached: a third station parks in pending_capacity, no job queued.
    third_uuid = "cccccccc-dddd-4eee-8fff-000000000000"
    monitoring._catalog._client.stations.append(rb_station(third_uuid, "Third FM"))  # type: ignore[attr-defined]
    third = monitoring.request_activation(third_uuid)
    assert third["actual_state"] == "pending_capacity"
    capacity = monitoring.capacity()
    assert capacity["can_add_station"] is False
    assert capacity["active_stations"] == 2
    assert "limit" in capacity["reason"].lower()


def test_activation_of_deleted_station_fails_closed(monitoring) -> None:
    with pytest.raises(MonitoringError) as error:
        monitoring.request_activation(DELETED_UUID)
    assert error.value.status_code == 410


def test_estimate_explicit(monitoring) -> None:
    selection = StationSelection.model_validate(
        {"mode": "explicit", "station_uuids": [HERTZ_UUID, PLAIN_UUID]}
    )
    estimate = monitoring.estimate_selection(selection)
    assert estimate["selected_stations"] == 2
    assert estimate["can_start_now"] == 2
    assert estimate["pending_capacity"] == 0


def test_estimate_country_all_disabled_by_default(monitoring, settings) -> None:
    assert settings.RADIO_ALLOW_COUNTRY_ALL is False
    selection = StationSelection.model_validate(
        {"mode": "country_all", "country_codes": ["DE"]}
    )
    with pytest.raises(MonitoringError) as error:
        monitoring.estimate_selection(selection)
    assert error.value.status_code == 403


def test_estimate_country_top_orders_by_votes(monitoring) -> None:
    selection = StationSelection.model_validate(
        {"mode": "country_top", "country_codes": ["DE"], "maximum_stations": 1}
    )
    estimate = monitoring.estimate_selection(selection)
    assert estimate["selected_stations"] == 1
    # Deleted FM has the most votes but is deleted; Hertz (50) wins over Plain (40).
    assert estimate["station_uuids_preview"] == [HERTZ_UUID]


def test_explicit_selection_over_campaign_cap_rejected(monitoring, settings) -> None:
    uuids = [f"aaaaaaa{i}-bbbb-4ccc-8ddd-eeeeeeeeee{i:02d}"[:36] for i in range(11)]
    selection = StationSelection.model_validate({"mode": "explicit", "station_uuids": uuids})
    with pytest.raises(MonitoringError) as error:
        monitoring.resolve_selection(selection)
    assert "RADIO_MAX_STATIONS_PER_CAMPAIGN" in error.value.detail


# -- shared reference counting -----------------------------------------------------


def _campaign(database, name: str) -> str:
    from datetime import datetime, timedelta, timezone

    from app.models import CampaignCreate

    payload = CampaignCreate.model_validate(
        {"name": name, "keywords": [{"value": "X" + name}], "station_ids": ["seed"]}
    )
    return database.create_campaign(payload, datetime.now(timezone.utc) - timedelta(days=1))


def test_shared_station_reference_counting(database, store, settings) -> None:
    managed_id = store.upsert_managed_station(
        {"station_uuid": PLAIN_UUID, "name": "Plain FM", "actual_state": "active"}
    )
    store.set_station_state(managed_id, actual_state="active", desired_state="active")
    campaign_a = _campaign(database, "Campaign A")
    campaign_b = _campaign(database, "Campaign B")
    store.set_campaign_members(campaign_a, [managed_id])
    store.add_campaign_station_ids(campaign_a, [local_station_id_for(PLAIN_UUID)])
    store.set_campaign_members(campaign_b, [managed_id])
    store.recompute_reference_counts(stop_grace_seconds=300)
    record = store.managed_station(managed_id)
    assert record is not None and record["active_campaign_count"] == 2
    assert record["stop_after_utc"] is None

    # Pause campaign A: still one reference, no stop scheduled.
    from app.models import CampaignUpdate

    database.update_campaign(campaign_a, CampaignUpdate(status="paused"))
    store.recompute_reference_counts(stop_grace_seconds=300)
    record = store.managed_station(managed_id)
    assert record is not None and record["active_campaign_count"] == 1
    assert record["stop_after_utc"] is None

    # Pause campaign B: zero references -> grace timer starts.
    database.update_campaign(campaign_b, CampaignUpdate(status="paused"))
    store.recompute_reference_counts(stop_grace_seconds=300)
    record = store.managed_station(managed_id)
    assert record is not None and record["active_campaign_count"] == 0
    assert record["stop_after_utc"] is not None
    assert store.stations_due_for_stop() == []  # grace period not over

    # Resume: timer cancelled.
    database.update_campaign(campaign_b, CampaignUpdate(status="active"))
    store.recompute_reference_counts(stop_grace_seconds=300)
    record = store.managed_station(managed_id)
    assert record is not None and record["stop_after_utc"] is None

    # Zero references with an elapsed grace -> due for stop.
    database.update_campaign(campaign_b, CampaignUpdate(status="paused"))
    store.recompute_reference_counts(stop_grace_seconds=0)
    due = store.stations_due_for_stop()
    assert [item["id"] for item in due] == [managed_id]


def test_legacy_pinned_station_never_scheduled_for_stop(store) -> None:
    legacy_id = store.import_legacy_station(
        local_station_id="hertz879", name="Hertz 87.9", country_code="DE",
        language_codes=["de", "en"],
    )
    store.recompute_reference_counts(stop_grace_seconds=0)
    record = store.managed_station(legacy_id)
    assert record is not None
    assert record["legacy_pinned"] is True
    assert record["stop_after_utc"] is None
    assert store.stations_due_for_stop() == []


def test_stop_refuses_pinned_and_referenced(monitoring, store, database) -> None:
    legacy_id = store.import_legacy_station(
        local_station_id="hertz879", name="Hertz 87.9", country_code="DE",
        language_codes=["de"],
    )
    with pytest.raises(MonitoringError) as pinned_error:
        monitoring.request_stop(legacy_id)
    assert pinned_error.value.status_code == 409

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    campaign = _campaign(database, "Ref campaign")
    store.set_campaign_members(campaign, [managed_id])
    store.recompute_reference_counts(stop_grace_seconds=300)
    with pytest.raises(MonitoringError) as ref_error:
        monitoring.request_stop(managed_id)
    assert "campaign" in ref_error.value.detail


# -- API surface (route-level, no stream URLs, OpenAPI gate) --------------------------


@pytest.fixture
def api_client(settings, store, catalog, monitoring) -> TestClient:
    from app.api.catalog_routes import router as catalog_router
    from app.services.preview import PreviewService

    app = FastAPI()
    app.include_router(catalog_router)
    app.state.settings = settings
    app.state.catalog_store = store
    app.state.catalog_service = catalog
    app.state.monitoring_service = monitoring
    app.state.preview_service = PreviewService(settings, store, catalog._client)  # type: ignore[arg-type]
    return TestClient(app)


def test_api_station_search_and_pagination_params(api_client) -> None:
    response = api_client.get(
        "/api/v1/radio-catalog/stations",
        params={"country_code": "DE", "limit": 2, "offset": 0},
    )
    assert response.status_code == 200
    page = response.json()
    assert page["limit"] == 2
    assert {"items", "offset", "limit", "has_more", "source", "mirror", "cache_age_seconds"} <= set(page)
    assert "stream" not in json.dumps(page)
    assert all("url" not in item or item.get("homepage_url") for item in page["items"])


def test_api_limit_capped_at_100(api_client) -> None:
    response = api_client.get("/api/v1/radio-catalog/stations", params={"limit": 101})
    assert response.status_code == 422


def test_api_countries(api_client) -> None:
    response = api_client.get("/api/v1/radio-catalog/countries")
    assert response.status_code == 200
    assert response.json()["items"][0]["code"] == "DE"


def test_api_capacity(api_client) -> None:
    response = api_client.get("/api/v1/monitoring/capacity")
    assert response.status_code == 200
    body = response.json()
    assert body["active_station_limit"] == 2
    assert body["can_add_station"] is True


def test_api_estimate_and_activate_flow(api_client) -> None:
    estimate = api_client.post(
        "/api/v1/monitoring/stations/estimate",
        json={"station_selection": {"mode": "explicit", "station_uuids": [PLAIN_UUID]}},
    )
    assert estimate.status_code == 200
    assert estimate.json()["can_start_now"] == 1

    activate = api_client.post(f"/api/v1/monitoring/stations/{PLAIN_UUID}/activate")
    assert activate.status_code == 200
    body = activate.json()
    assert body["actual_state"] == "pending_probe"
    assert body["job_id"] is not None

    status = api_client.get(f"/api/v1/monitoring/stations/{body['managed_station_id']}/status")
    assert status.status_code == 200
    assert status.json()["actual_state"] == "pending_probe"
    assert "stream_url" not in json.dumps(status.json())


def test_api_preview_token_for_deleted_station_is_410(api_client) -> None:
    response = api_client.post(
        f"/api/v1/radio-catalog/stations/{DELETED_UUID}/preview-token"
    )
    assert response.status_code == 410


def test_openapi_contains_all_gate_paths(api_client) -> None:
    schema = api_client.app.openapi()  # type: ignore[attr-defined]
    paths = set(schema["paths"])
    required = {
        "/api/v1/radio-catalog/countries",
        "/api/v1/radio-catalog/languages",
        "/api/v1/radio-catalog/tags",
        "/api/v1/radio-catalog/codecs",
        "/api/v1/radio-catalog/stations",
        "/api/v1/radio-catalog/stations/{station_uuid}",
        "/api/v1/radio-catalog/stations/{station_uuid}/probe",
        "/api/v1/radio-catalog/stations/{station_uuid}/preview-token",
        "/api/v1/radio-catalog/preview/{token}",
        "/api/v1/monitoring/capacity",
        "/api/v1/monitoring/stations",
        "/api/v1/monitoring/stations/estimate",
        "/api/v1/monitoring/stations/{station_uuid}/activate",
        "/api/v1/monitoring/stations/{managed_station_id}/stop",
        "/api/v1/monitoring/stations/{managed_station_id}/status",
    }
    missing = required - paths
    assert not missing, f"gate endpoints missing: {missing}"
