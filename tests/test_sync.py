from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.models import CampaignCreate
from app.services.stations import StationService
from app.services.sync import IntelligenceSyncService


def test_sync_materializes_campaign_mention(settings, database, fake_s3) -> None:
    (settings.RADIO_STATION_CONFIG_DIR / "hertz879.env").write_text(
        'STATION_ID="hertz879"\nSTATION_NAME="Hertz 87.9"\nSTATION_LANGUAGE="de"\n'
    )
    payload = CampaignCreate.model_validate(
        {
            "name": "Supersuckers test",
            "keywords": [{"value": "Supersuckers", "aliases": ["Super Suckers"]}],
            "station_ids": ["hertz879"],
            "backfill_days": 7,
        }
    )
    database.create_campaign(payload, datetime.now(timezone.utc) - timedelta(days=1))
    binding = database.active_bindings()[0]
    now = datetime.now(timezone.utc)
    result_key = "results/intelligence/hertz879/2026/07/13/test.intelligence.json"
    fake_s3.put_json(
        result_key,
        {
            "source": {
                "station_id": "hertz879",
                "station_name": "Hertz 87.9",
                "broadcast_start_utc": (now - timedelta(seconds=10)).isoformat(),
                "broadcast_end_utc": now.isoformat(),
                "detected_language": "en",
                "language_probability": 0.997,
                "audio_s3_uri": "s3://bucket/clean-speech/hertz879/test.wav",
            },
            "mentions": [
                {
                    "mention_id": "source-1",
                    "entity_id": binding["entity_id"],
                    "display_name": "Supersuckers",
                    "matched_alias": "Super Suckers",
                    "context": "The greatest rock band in the world, the Super Suckers.",
                    "broadcast_start_utc": (now - timedelta(seconds=5)).isoformat(),
                    "broadcast_end_utc": (now - timedelta(seconds=3)).isoformat(),
                    "audio_s3_uri": "s3://bucket/clean-speech/hertz879/test.wav",
                    "sentiment": {"label": "positive", "score": 0.73, "margin": 0.56},
                }
            ],
        },
    )
    service = IntelligenceSyncService(
        settings,
        database,
        StationService(settings, fake_s3),
        fake_s3,
    )
    result = asyncio.run(service.sync_once())
    mentions, total = database.list_mentions()
    assert result["mentions_materialized"] == 1
    assert total == 1
    assert mentions[0]["keyword"] == "Supersuckers"
    assert mentions[0]["sentiment"]["label"] == "positive"
    assert mentions[0]["playback_start_seconds"] >= 0


def test_sync_is_idempotent(settings, database, fake_s3) -> None:
    payload = CampaignCreate.model_validate(
        {"name": "Watch", "keywords": [{"value": "Brand"}], "station_ids": ["hertz879"]}
    )
    database.create_campaign(payload, datetime.now(timezone.utc) - timedelta(days=1))
    fake_s3.put_json(
        "results/intelligence/hertz879/one.json",
        {"source": {"station_id": "hertz879"}, "mentions": []},
    )
    service = IntelligenceSyncService(
        settings,
        database,
        StationService(settings, fake_s3),
        fake_s3,
    )
    first = asyncio.run(service.sync_once())
    second = asyncio.run(service.sync_once())
    assert first["objects_loaded"] == 1
    assert second["objects_loaded"] == 0
