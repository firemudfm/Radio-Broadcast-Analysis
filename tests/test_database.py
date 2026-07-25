from __future__ import annotations

from datetime import datetime, timezone

from app.models import CampaignCreate


def test_campaign_round_trip(database) -> None:
    payload = CampaignCreate.model_validate(
        {
            "name": "Supersuckers test",
            "objective": "brand_mentions",
            "keywords": [{"value": "Supersuckers", "aliases": ["Super Suckers"]}],
            "station_ids": ["hertz879"],
        }
    )
    campaign_id = database.create_campaign(payload, datetime.now(timezone.utc))
    campaign = database.get_campaign(campaign_id)
    assert campaign is not None
    assert campaign["name"] == "Supersuckers test"
    assert campaign["station_ids"] == ["hertz879"]
    assert campaign["keywords"][0]["value"] == "Supersuckers"
    assert database.campaign_revision() == 1


def test_delete_campaign_cascades(database) -> None:
    payload = CampaignCreate.model_validate(
        {"name": "Delete me", "keywords": [{"value": "Brand"}], "station_ids": ["hertz879"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(timezone.utc))
    assert database.delete_campaign(campaign_id) is True
    assert database.get_campaign(campaign_id) is None

def test_mention_window_days_bounds_summary_and_campaign_counts(settings, database) -> None:
    from datetime import timedelta

    from app.db import Database, iso, utc_now
    from app.models import CampaignCreate

    payload = CampaignCreate.model_validate(
        {"name": "Window watch", "keywords": [{"value": "Brand"}], "station_ids": ["hertz879"]}
    )
    campaign_id = database.create_campaign(payload, utc_now() - timedelta(days=10))
    binding = database.active_bindings()[0]

    def record(source_id: str, start) -> dict:
        stamp = iso(start)
        return {
            "campaign_id": campaign_id,
            "campaign_keyword_id": binding["keyword_id"],
            "station_id": "hertz879",
            "station_name": "Hertz 87.9",
            "station_country_code": "DE",
            "station_language_codes": ["de"],
            "source_result_s3_key": f"results/intelligence/{source_id}.json",
            "source_mention_id": source_id,
            "entity_id": binding["entity_id"],
            "display_name": "Brand",
            "matched_alias": "Brand",
            "context": "Brand was mentioned.",
            "detected_language": "en",
            "language_probability": 0.9,
            "sentiment_label": "positive",
            "sentiment_score": 0.9,
            "sentiment_margin": 0.4,
            "needs_review": False,
            "broadcast_start_utc": stamp,
            "broadcast_end_utc": stamp,
            "audio_clip_start_utc": stamp,
            "audio_clip_end_utc": stamp,
            "audio_s3_key": f"clean-speech/hertz879/{source_id}.wav",
            "raw_audio_s3_key": None,
            "transcript_s3_key": None,
        }

    database.upsert_mention(record("recent", utc_now() - timedelta(hours=2)))
    database.upsert_mention(record("stale", utc_now() - timedelta(days=3)))

    # Default 7-day window sees both mentions.
    assert database.sentiment_summary()["positive"] == 2
    assert database.get_campaign(campaign_id)["mentions_7d"] == 2

    # A 1-day window only counts the mention from the last 24 hours.
    narrow = Database(settings.RADIO_DATABASE_PATH, mention_window_days=1)
    narrow.connect()
    try:
        assert narrow.sentiment_summary()["positive"] == 1
        assert narrow.get_campaign(campaign_id)["mentions_7d"] == 1
    finally:
        narrow.close()

def test_mention_audio_pad_expands_playback_to_full_clip(settings, database) -> None:
    from datetime import timedelta

    from app.db import Database, iso, utc_now
    from app.models import CampaignCreate

    payload = CampaignCreate.model_validate(
        {"name": "Pad watch", "keywords": [{"value": "Brand"}], "station_ids": ["hertz879"]}
    )
    campaign_id = database.create_campaign(payload, utc_now() - timedelta(days=1))
    binding = database.active_bindings()[0]
    clip_start = utc_now() - timedelta(hours=1)
    clip_end = clip_start + timedelta(seconds=60)
    kw_start = clip_start + timedelta(seconds=30)
    kw_end = clip_start + timedelta(seconds=31)
    mention_id = database.upsert_mention(
        {
            "campaign_id": campaign_id,
            "campaign_keyword_id": binding["keyword_id"],
            "station_id": "hertz879",
            "station_name": "Hertz 87.9",
            "station_country_code": "DE",
            "station_language_codes": ["de"],
            "source_result_s3_key": "results/intelligence/pad.json",
            "source_mention_id": "pad-1",
            "entity_id": binding["entity_id"],
            "display_name": "Brand",
            "matched_alias": "Brand",
            "context": "Brand in a one minute discussion.",
            "detected_language": "en",
            "language_probability": 0.9,
            "sentiment_label": "positive",
            "sentiment_score": 0.9,
            "sentiment_margin": 0.4,
            "needs_review": False,
            "broadcast_start_utc": iso(kw_start),
            "broadcast_end_utc": iso(kw_end),
            "audio_clip_start_utc": iso(clip_start),
            "audio_clip_end_utc": iso(clip_end),
            "audio_s3_key": "clean-speech/hertz879/pad.wav",
            "raw_audio_s3_key": None,
            "transcript_s3_key": None,
        }
    )

    # Default 2s padding: a short window around the keyword.
    default_view = database.mention_view_by_id(mention_id)
    assert default_view is not None
    assert default_view["playback_start_seconds"] == 28.0
    assert default_view["playback_end_seconds"] == 33.0

    # Large padding plays the whole captured discussion segment.
    wide = Database(settings.RADIO_DATABASE_PATH, mention_audio_pad_seconds=900.0)
    wide.connect()
    try:
        wide_view = wide.mention_view_by_id(mention_id)
        assert wide_view is not None
        assert wide_view["playback_start_seconds"] == 0.0
        assert wide_view["playback_end_seconds"] == 60.0
        assert wide_view["audio_duration_seconds"] == 60.0
    finally:
        wide.close()
