from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.models import CampaignCreate
from app.services.audio import AudioService


class FakeRequest:
    def url_for(self, name: str, **values) -> str:
        assert name == "stream_audio"
        return f"http://127.0.0.1:8788/api/v1/brand-signal/audio/{values['token']}"


def test_audio_token_and_range(settings, database, fake_s3) -> None:
    payload = CampaignCreate.model_validate(
        {"name": "Watch", "keywords": [{"value": "Brand"}], "station_ids": ["hertz879"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(UTC))
    binding = database.active_bindings()[0]
    record = {
        "campaign_id": campaign_id,
        "campaign_keyword_id": binding["keyword_id"],
        "station_id": "hertz879",
        "station_name": "Hertz 87.9",
        "station_country_code": "DE",
        "station_language_codes": ["de"],
        "source_result_s3_key": "results/intelligence/test.json",
        "source_mention_id": "source-1",
        "entity_id": binding["entity_id"],
        "display_name": "Brand",
        "matched_alias": "Brand",
        "context": "Brand was mentioned.",
        "detected_language": "en",
        "language_probability": 0.9,
        "sentiment_label": "neutral",
        "sentiment_score": 0.6,
        "sentiment_margin": 0.2,
        "needs_review": False,
        "broadcast_start_utc": "2026-07-13T01:00:01Z",
        "broadcast_end_utc": "2026-07-13T01:00:02Z",
        "audio_clip_start_utc": "2026-07-13T01:00:00Z",
        "audio_clip_end_utc": "2026-07-13T01:00:04Z",
        "audio_s3_key": "clean-speech/hertz879/test.wav",
        "raw_audio_s3_key": None,
        "transcript_s3_key": None,
    }
    mention_id = database.upsert_mention(record)
    fake_s3.put_bytes("clean-speech/hertz879/test.wav", b"0123456789")
    service = AudioService(settings, database, fake_s3)
    token_result = service.create_token(mention_id, FakeRequest())
    token = token_result["url"].rsplit("/", 1)[-1]
    response = service.stream(token, "bytes=2-5")
    async def collect() -> bytes:
        chunks = [chunk async for chunk in response.body_iterator]
        return b"".join(chunks)

    assert response.status_code == 206
    assert asyncio.run(collect()) == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
