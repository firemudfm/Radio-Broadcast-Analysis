from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

# Station capacity is governed by two independent systems:
#
#   * the legacy systemd pipeline, sized by RADIO_MAX_ACTIVE_STATIONS
#   * the v0.5 shared-SQS pipeline, sized by RADIO_MAX_ACTIVE_UNIQUE_STATIONS
#     together with RADIO_LISTENER_MAX_SESSIONS
#
# Their defaults differ on purpose (2 versus 8) because they size different
# runtimes. These tests exist so the two knobs are never collapsed into one
# another again, in either direction.
CAPACITY_ENV_VARS = (
    "RADIO_PIPELINE_MODE",
    "RADIO_MAX_ACTIVE_STATIONS",
    "RADIO_MAX_ACTIVE_UNIQUE_STATIONS",
    "RADIO_LISTENER_MAX_SESSIONS",
)


@pytest.fixture
def clean_capacity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop capacity overrides so a developer shell or CI runner cannot mask a regression."""
    for name in CAPACITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def build_settings(tmp_path: Path) -> Settings:
    """Build Settings supplying only the values that have no safe default."""
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_AUDIO_TOKEN_SECRET="t" * 48,
    )


@pytest.mark.usefixtures("clean_capacity_env")
def test_default_pipeline_mode_is_legacy(tmp_path: Path) -> None:
    assert build_settings(tmp_path).RADIO_PIPELINE_MODE == "legacy"


@pytest.mark.usefixtures("clean_capacity_env")
def test_legacy_station_capacity_default_is_two(tmp_path: Path) -> None:
    assert build_settings(tmp_path).RADIO_MAX_ACTIVE_STATIONS == 2


@pytest.mark.usefixtures("clean_capacity_env")
def test_shared_sqs_capacity_defaults_are_eight(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)

    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 8
    assert settings.RADIO_LISTENER_MAX_SESSIONS == 8


@pytest.mark.usefixtures("clean_capacity_env")
def test_legacy_and_shared_capacity_defaults_are_independent(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)

    assert settings.RADIO_MAX_ACTIVE_STATIONS == 2
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 8
    assert settings.RADIO_MAX_ACTIVE_STATIONS != settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS


@pytest.mark.usefixtures("clean_capacity_env")
def test_legacy_capacity_override_does_not_move_shared_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RADIO_MAX_ACTIVE_STATIONS", "6")

    settings = build_settings(tmp_path)

    assert settings.RADIO_MAX_ACTIVE_STATIONS == 6
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 8
    assert settings.RADIO_LISTENER_MAX_SESSIONS == 8


@pytest.mark.usefixtures("clean_capacity_env")
def test_shared_capacity_override_does_not_move_legacy_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RADIO_MAX_ACTIVE_UNIQUE_STATIONS", "32")

    settings = build_settings(tmp_path)

    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 32
    assert settings.RADIO_LISTENER_MAX_SESSIONS == 8
    assert settings.RADIO_MAX_ACTIVE_STATIONS == 2
