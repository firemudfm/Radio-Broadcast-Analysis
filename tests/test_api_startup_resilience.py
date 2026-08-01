"""The API must finish starting even when S3 is unreachable.

This is a deployment concern, not a cosmetic one. ``deploy-compose.sh`` and
``rollback-compose.sh`` both wait on the container health gate before they will
move a symlink, and the health check asks the server for ``/readyz``. A process
that dies inside ``lifespan`` never answers, so an unreachable S3 -- a transient
outage, an instance profile that has not attached yet, a credential-less local
run -- used to turn into "deploy failed" *and* "rollback failed", which is the
worst possible moment to lose the ability to roll back.

The rule this file pins down: start-up may log an S3 failure, but must not die
of one. Routes that genuinely need S3 still fail loudly to their own callers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


class NoCredentialsS3:
    """Stands in for botocore with nothing to sign with.

    botocore raises ``NoCredentialsError`` while *signing*, before any socket is
    opened, so this stub reproduces the real failure without a network at all.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def _explode(*args, **kwargs):
            self.calls.append(name)
            raise RuntimeError("Unable to locate credentials")

        return _explode


@pytest.fixture
def startup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    station_dir = tmp_path / "stations"
    station_dir.mkdir()
    metadata = tmp_path / "stations.json"
    metadata.write_text(
        json.dumps({"stations": [{"id": "hertz879", "name": "Hertz 87.9"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RADIO_S3_BUCKET", "bucket-that-does-not-exist")
    monkeypatch.setenv("RADIO_DATABASE_PATH", str(tmp_path / "radio.db"))
    monkeypatch.setenv("RADIO_AUDIO_TOKEN_SECRET", "x" * 48)
    monkeypatch.setenv("RADIO_STATION_CONFIG_DIR", str(station_dir))
    monkeypatch.setenv("RADIO_STATION_METADATA_PATH", str(metadata))
    monkeypatch.setenv("RADIO_SYNC_ENABLED", "false")
    monkeypatch.setenv("RADIO_SYNC_ON_STARTUP", "false")
    monkeypatch.setenv("RADIO_PIPELINE_MODE", "legacy")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _client(monkeypatch: pytest.MonkeyPatch, s3: NoCredentialsS3) -> TestClient:
    import app.main as main

    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: s3)
    return TestClient(main.app)


def test_startup_survives_s3_without_credentials(
    startup_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: this used to abort with ``Application startup failed``."""
    s3 = NoCredentialsS3()
    with _client(monkeypatch, s3) as client:
        # Snapshot before any request: /healthz probes S3 too, so counting calls
        # afterwards would prove nothing about what start-up did.
        startup_calls = list(s3.calls)
        body = client.get("/healthz").json()

    # Proves the failure was really provoked rather than quietly skipped -- the
    # legacy station import did reach for S3 during lifespan, and did blow up.
    assert "list_objects_v2" in startup_calls
    # And the server is honest about it rather than pretending S3 is fine.
    assert body["s3"] == "error"


def test_readyz_answers_when_s3_is_down(
    startup_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The health gate the deploy and rollback scripts wait on must respond."""
    with _client(monkeypatch, NoCredentialsS3()) as client:
        response = client.get("/readyz")
    # 200 or 503 are both legitimate answers; a dead process answers neither.
    assert response.status_code in (200, 503)
    assert "ready" in response.json()


def test_no_pinned_stations_never_touches_s3(
    startup_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing pinned there is nothing to import, so nothing to fetch."""
    monkeypatch.setenv("RADIO_LEGACY_PINNED_STATION_IDS", "")
    get_settings.cache_clear()
    s3 = NoCredentialsS3()
    with _client(monkeypatch, s3) as client:
        startup_calls = list(s3.calls)
        assert client.get("/healthz").status_code == 200
    assert startup_calls == []


def test_s3_backed_route_still_reports_its_failure(
    startup_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tolerating the failure at start-up must not mask it at request time.

    If this ever starts returning 200 with empty data, the guard has turned a
    loud outage into silent data loss.
    """
    with _client(monkeypatch, NoCredentialsS3()) as client:
        with pytest.raises(Exception):  # noqa: B017 - any propagated failure is correct
            client.get("/api/v1/brand-signal/campaigns")
