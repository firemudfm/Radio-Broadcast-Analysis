from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import CampaignCreate


def test_campaign_input_deduplicates_stations_and_keywords() -> None:
    payload = CampaignCreate.model_validate(
        {
            "name": "Watch",
            "keywords": [
                {"value": "Supersuckers"},
                {"value": "supersuckers"},
            ],
            "station_ids": ["hertz879", "hertz879"],
        }
    )
    assert payload.station_ids == ["hertz879"]
    assert [item.value for item in payload.keywords] == ["Supersuckers"]


def test_campaign_requires_station_and_keyword() -> None:
    with pytest.raises(ValidationError):
        CampaignCreate.model_validate({"name": "Watch", "keywords": [], "station_ids": []})


def test_settings_accepts_csv_cors_origins(monkeypatch, tmp_path) -> None:
    from app.config import Settings

    monkeypatch.setenv("RADIO_S3_BUCKET", "bucket")
    monkeypatch.setenv("RADIO_AUDIO_TOKEN_SECRET", "x" * 48)
    monkeypatch.setenv(
        "RADIO_API_CORS_ORIGINS",
        "http://localhost:5175,http://127.0.0.1:5175",
    )
    monkeypatch.setenv("RADIO_DATABASE_PATH", str(tmp_path / "radio.db"))
    settings = Settings()
    assert settings.RADIO_API_CORS_ORIGINS == [
        "http://localhost:5175",
        "http://127.0.0.1:5175",
    ]
