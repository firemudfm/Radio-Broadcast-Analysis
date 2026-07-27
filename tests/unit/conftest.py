"""Fixtures for the shared-pipeline unit tests.

Deliberately independent of the legacy `tests/conftest.py` fixtures so the two
suites cannot couple: the baseline 140 tests are a regression gate and must keep
passing untouched.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.pipeline.local_segment_store import LocalSegmentStore
from app.pipeline.queue import MemoryQueue


@pytest.fixture
def pipeline_settings(tmp_path: Path) -> Settings:
    """A `shared_sqs` configuration pointing at temporary directories."""
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_SPOOL_PATH=tmp_path / "spool",
        RADIO_MODEL_PATH=tmp_path / "models",
        RADIO_EVIDENCE_PATH=tmp_path / "evidence",
        RADIO_LOG_PATH=tmp_path / "logs",
        RADIO_PIPELINE_MODE="shared_sqs",
        RADIO_QUEUE_BACKEND="memory",
        RADIO_SEGMENT_STORE="local",
        RADIO_SYNC_ENABLED=False,
        RADIO_ASR_BACKEND="fake",
    )


@pytest.fixture
def pipeline_database(pipeline_settings: Settings) -> Database:
    database = Database(pipeline_settings.RADIO_DATABASE_PATH)
    database.connect()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def spool(tmp_path: Path) -> LocalSegmentStore:
    store = LocalSegmentStore(tmp_path / "spool")
    store.ensure_root()
    return store


@pytest.fixture
def memory_queue() -> MemoryQueue:
    return MemoryQueue("radio-test.fifo", visibility_seconds=30)


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Manually advanced monotonic clock for visibility/backoff tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
