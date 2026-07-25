from __future__ import annotations

import json
from datetime import UTC, datetime

from app.models import CampaignCreate
from app.services.keywords import KeywordConfigService


def test_publish_preserves_manual_entities(settings, database, fake_s3) -> None:
    fake_s3.put_json(
        settings.RADIO_KEYWORDS_KEY,
        {
            "schema_version": "1.0",
            "entities": [
                {
                    "id": "manual",
                    "display_name": "Manual",
                    "enabled": True,
                    "match_mode": "tokens",
                    "aliases": {"*": ["Manual"]},
                }
            ],
        },
    )
    payload = CampaignCreate.model_validate(
        {
            "name": "Watch",
            "keywords": [{"value": "Supersuckers", "aliases": ["Super Suckers"]}],
            "station_ids": ["hertz879"],
        }
    )
    database.create_campaign(payload, datetime.now(UTC))
    result = KeywordConfigService(settings, database, fake_s3).publish()
    body = json.loads(fake_s3.objects[settings.RADIO_KEYWORDS_KEY]["Body"])
    assert result["manual_entities"] == 1
    assert result["managed_entities"] == 1
    assert {item["id"] for item in body["entities"]} >= {"manual"}
    managed = next(item for item in body["entities"] if item.get("managed_by"))
    assert managed["aliases"]["*"] == ["Supersuckers", "Super Suckers"]
