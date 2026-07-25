from __future__ import annotations

from app.services.stations import StationService


def test_station_service_reads_local_config(settings, fake_s3) -> None:
    (settings.RADIO_STATION_CONFIG_DIR / "hertz879.env").write_text(
        'STATION_ID="hertz879"\nSTATION_NAME="Hertz 87.9"\nSTATION_LANGUAGE="de"\n'
    )
    stations = StationService(settings, fake_s3).list_stations()
    assert stations == [
        {
            "id": "hertz879",
            "name": "Hertz 87.9",
            "country_code": "DE",
            "language_codes": ["de", "en"],
            "connected": True,
            "enabled": True,
        }
    ]
