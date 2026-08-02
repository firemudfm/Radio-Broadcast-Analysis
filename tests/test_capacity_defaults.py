"""Capacity defaults: three numbers that must never be collapsed into one.

The old two-pipeline design had two capacity knobs sizing two runtimes. There
is one runtime now, and one way left to get this wrong: reading "we support
1,000 stations" as a statement about live decoding.

    RADIO_MAX_REQUESTED_UNIQUE_STATIONS   control plane. Rows, campaign
        mappings and keyword indexes. Proven to 1,000 by the load suite.

    RADIO_MAX_ACTIVE_UNIQUE_STATIONS      compute. One ffmpeg decode, one ring
        buffer and a share of ASR each. **1** on this host.

    RADIO_LISTENER_MAX_SESSIONS           process-local. Concurrent listener
        sessions, bounded by sockets and threads.

Requesting a station is cheap; decoding one is not. Stations above the active
limit are parked as `pending_capacity`, which is a queue rather than a refusal.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

CAPACITY_ENV_VARS = (
    "RADIO_MAX_REQUESTED_UNIQUE_STATIONS",
    "RADIO_MAX_ACTIVE_UNIQUE_STATIONS",
    "RADIO_LISTENER_MAX_SESSIONS",
    "RADIO_LISTENER_SHARD_COUNT",
    "RADIO_LISTENER_SHARD_INDEX",
    # Removed settings. A developer shell may still export one, and the startup
    # guard would then fire here rather than in the test that owns it.
    "RADIO_PIPELINE_MODE",
    "RADIO_MAX_ACTIVE_STATIONS",
)


@pytest.fixture
def clean_capacity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop overrides so a developer shell or CI runner cannot mask a regression."""
    for name in CAPACITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def build(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="bucket",
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        **overrides,
    )


pytestmark = pytest.mark.usefixtures("clean_capacity_env")


def test_active_capacity_defaults_to_one(tmp_path: Path) -> None:
    """A 4 vCPU aarch64 host runs ASR, an LLM and the API. One live station is
    the only figure this deployment has been verified for."""
    settings = build(tmp_path)
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 1
    assert settings.RADIO_LISTENER_MAX_SESSIONS == 1


def test_requested_capacity_defaults_to_one_thousand(tmp_path: Path) -> None:
    settings = build(tmp_path)
    assert settings.RADIO_MAX_REQUESTED_UNIQUE_STATIONS == 1000


def test_requested_and_active_capacity_are_a_thousandfold_apart(tmp_path: Path) -> None:
    """The gap is the entire point: asking is cheap, decoding is not."""
    settings = build(tmp_path)
    assert settings.RADIO_MAX_REQUESTED_UNIQUE_STATIONS == 1000
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 1


def test_raising_requested_capacity_does_not_move_active_capacity(tmp_path: Path) -> None:
    settings = build(tmp_path, RADIO_MAX_REQUESTED_UNIQUE_STATIONS=5000)
    assert settings.RADIO_MAX_REQUESTED_UNIQUE_STATIONS == 5000
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 1


def test_active_capacity_cannot_exceed_requested_capacity(tmp_path: Path) -> None:
    """A station cannot be decoded without first having been requested."""
    with pytest.raises(ValueError, match="cannot exceed"):
        build(
            tmp_path,
            RADIO_MAX_REQUESTED_UNIQUE_STATIONS=4,
            RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8,
            RADIO_LISTENER_MAX_SESSIONS=8,
        )


def test_listener_sessions_cannot_exceed_active_capacity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RADIO_LISTENER_MAX_SESSIONS cannot exceed"):
        build(tmp_path, RADIO_MAX_ACTIVE_UNIQUE_STATIONS=2, RADIO_LISTENER_MAX_SESSIONS=4)


@pytest.mark.parametrize("value", [0, 513])
def test_active_capacity_is_bounded(tmp_path: Path, value: int) -> None:
    """An unbounded station count is how a host is silently oversubscribed
    until audio starts being dropped."""
    with pytest.raises(ValueError):
        build(tmp_path, RADIO_MAX_ACTIVE_UNIQUE_STATIONS=value)


@pytest.mark.parametrize("value", [0, 10_001])
def test_requested_capacity_is_bounded(tmp_path: Path, value: int) -> None:
    with pytest.raises(ValueError):
        build(tmp_path, RADIO_MAX_REQUESTED_UNIQUE_STATIONS=value)


def test_tests_and_future_hosts_may_raise_active_capacity(tmp_path: Path) -> None:
    """The default is conservative, not a ceiling."""
    settings = build(
        tmp_path, RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8, RADIO_LISTENER_MAX_SESSIONS=8
    )
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 8


def test_a_shard_index_must_exist_within_the_shard_count(tmp_path: Path) -> None:
    """Shard 2 of 2 does not exist, and a listener claiming it owns nothing."""
    with pytest.raises(ValueError, match="RADIO_LISTENER_SHARD_INDEX"):
        build(tmp_path, RADIO_LISTENER_SHARD_COUNT=2, RADIO_LISTENER_SHARD_INDEX=2)
