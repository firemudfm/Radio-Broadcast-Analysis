"""Fixtures for end-to-end shared-pipeline integration tests.

These run the *real* workers -- planner, listener persistence, transcription,
analysis -- against the in-memory FIFO queue and the local spool. Only the two
heavyweight external dependencies are substituted (ASR and the LLM), and both
substitutes implement the full contract rather than being mocks, so the wiring
under test is production wiring.
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.db import Database
from app.pipeline.factory import (
    ANALYSIS_QUEUE,
    TRANSCRIPTION_QUEUE,
    build_queue,
    reset_memory_queues,
)
from app.pipeline.local_segment_store import LocalSegmentStore
from app.services.llm_analysis import FakeLlmClient
from app.services.transcription import FakeTranscriptionEngine, TranscriptionService


@pytest.fixture(autouse=True)
def _isolated_queues():
    reset_memory_queues()
    yield
    reset_memory_queues()


@pytest.fixture
def settings(tmp_path) -> Settings:
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
        RADIO_ASR_BACKEND="fake",
        RADIO_SYNC_ENABLED=False,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def spool(settings: Settings) -> LocalSegmentStore:
    store = LocalSegmentStore(settings.RADIO_SPOOL_PATH)
    store.ensure_root()
    return store


@pytest.fixture
def queues(settings: Settings) -> dict:
    return {
        TRANSCRIPTION_QUEUE: build_queue(settings, TRANSCRIPTION_QUEUE),
        ANALYSIS_QUEUE: build_queue(settings, ANALYSIS_QUEUE),
    }


@pytest.fixture
def transcription_service(settings: Settings):
    """Returns a fixed transcript for every segment."""

    def build(text: str) -> TranscriptionService:
        engine = FakeTranscriptionEngine(responses={"*": text}, language="en")
        return TranscriptionService(settings, engine=engine, confirmation_engine=engine)

    return build


@pytest.fixture
def llm_client():
    def build(payload: str) -> FakeLlmClient:
        return FakeLlmClient(responses=[payload])

    return build


@pytest.fixture
def analysis_worker(settings: Settings, database: Database, queues: dict):
    """A real AnalysisWorker with only the LLM and S3 substituted.

    The consumer-side reconstruction is the code under test, so everything
    between `parse_analysis_job` and the persisted row stays production code.
    """
    from app.pipeline.factory import ANALYSIS_QUEUE as _ANALYSIS_QUEUE
    from app.services.llm_analysis import ConversationAnalyzer
    from app.workers.analysis import AnalysisWorker

    payload = json.dumps(
        {
            "content_type": "advertisement",
            "language": "hi",
            "relevant": True,
            "summary": "An advertisement.",
            "sentiment": "positive",
            "confidence": 0.8,
        }
    )
    return AnalysisWorker(
        settings,
        database,
        queue=queues[_ANALYSIS_QUEUE],
        analyzer=ConversationAnalyzer(settings, client=FakeLlmClient(responses=[payload])),
        s3_client=None,
    )
