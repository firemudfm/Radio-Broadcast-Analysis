from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.models import CampaignCreate, CampaignView
from app.services.campaigns import CampaignService


class FakeStations:
    def station_map(self):
        return {
            "hertz879": {
                "id": "hertz879",
                "name": "Hertz 87.9",
                "country_code": "DE",
                "language_codes": ["de", "en"],
                "connected": True,
                "enabled": True,
            }
        }

    def list_stations(self):
        return list(self.station_map().values())


class FakeKeywords:
    def publish(self):
        return {"status": "ok"}


class FakeSync:
    async def sync_once(self):
        return {"objects_scanned": 0}


def test_hydrate_removes_internal_station_ids(database) -> None:
    payload = CampaignCreate.model_validate(
        {
            "name": "Supersuckers Frontend Test",
            "objective": "brand_mentions",
            "keywords": [
                {"value": "Supersuckers", "aliases": ["Super Suckers"]}
            ],
            "station_ids": ["hertz879"],
        }
    )
    database.create_campaign(payload, datetime.now(timezone.utc))
    service = CampaignService(database, FakeStations(), FakeKeywords(), FakeSync())

    campaigns = asyncio.run(service.list_campaigns())

    assert len(campaigns) == 1
    assert "station_ids" not in campaigns[0]
    assert campaigns[0]["stations"][0]["id"] == "hertz879"
    # This is the exact response-model validation that failed in v0.2.0.
    validated = CampaignView.model_validate(campaigns[0])
    assert validated.name == "Supersuckers Frontend Test"
    assert validated.keywords[0].keyword_type == "brand"
    assert validated.keywords[0].semantic_matching is False
    assert validated.keywords[0].semantic_threshold == 0.74


def test_hydrate_does_not_mutate_input_mapping(database) -> None:
    service = CampaignService(database, FakeStations(), FakeKeywords(), FakeSync())
    raw = {
        "id": "campaign-1",
        "name": "Watch",
        "objective": "brand_mentions",
        "business_name": None,
        "business_description": None,
        "status": "active",
        "monitor_from_utc": "2026-07-13T00:00:00Z",
        "station_ids": ["hertz879"],
        "keywords": [],
        "mentions_7d": 0,
        "created_at": "2026-07-13T00:00:00Z",
        "updated_at": "2026-07-13T00:00:00Z",
    }

    hydrated = asyncio.run(service._hydrate([raw]))

    assert raw["station_ids"] == ["hertz879"]
    assert "station_ids" not in hydrated[0]
