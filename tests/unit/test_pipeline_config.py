"""Configuration contract for the pipeline modes (ADR-001)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

BASE = {"RADIO_S3_BUCKET": "bucket", "RADIO_AUDIO_TOKEN_SECRET": "x" * 40}


def test_there_is_no_pipeline_mode_setting() -> None:
    """One pipeline means no switch. A mode field would immediately grow a
    second runtime branch behind it."""
    assert "RADIO_PIPELINE_MODE" not in Settings.model_fields
    assert not hasattr(Settings(**BASE), "shared_pipeline_enabled")


def test_a_stale_legacy_environment_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`extra="ignore"` would silently drop this, so an operator whose
    application.env still says legacy would get the shared pipeline anyway and
    never learn their file was stale."""
    monkeypatch.setenv("RADIO_PIPELINE_MODE", "legacy")
    with pytest.raises(ValidationError, match="no longer exist"):
        Settings(**BASE)


def test_the_migration_message_names_the_setting_and_the_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RADIO_MAX_ACTIVE_STATIONS", "4")
    with pytest.raises(ValidationError) as error:
        Settings(**BASE)
    message = str(error.value)
    assert "RADIO_MAX_ACTIVE_STATIONS" in message
    assert "RADIO_MAX_ACTIVE_UNIQUE_STATIONS" in message


def test_shared_sqs_requires_queue_urls() -> None:
    with pytest.raises(ValidationError, match="RADIO_TRANSCRIPTION_QUEUE_URL is required"):
        Settings(**BASE, RADIO_QUEUE_BACKEND="sqs")


def test_shared_sqs_requires_fifo_queues() -> None:
    """Ordering per station is correctness, not preference."""
    with pytest.raises(ValidationError, match="FIFO"):
        Settings(
            **BASE,
            RADIO_QUEUE_BACKEND="sqs",
            RADIO_TRANSCRIPTION_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/plain",
            RADIO_ANALYSIS_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/a.fifo",
        )


def test_the_memory_backend_needs_no_queue_urls() -> None:
    """compose.dev and the test suite run without AWS. The memory queue is a
    test double, and production is barred from it by APP_ENV."""
    settings = Settings(**BASE, RADIO_QUEUE_BACKEND="memory")
    assert settings.RADIO_QUEUE_BACKEND == "memory"


def test_production_refuses_the_memory_queue() -> None:
    """It loses every message on restart, so the system would look healthy
    while producing nothing."""
    with pytest.raises(ValidationError, match="RADIO_QUEUE_BACKEND=sqs"):
        Settings(**BASE, APP_ENV="production", RADIO_QUEUE_BACKEND="memory")


def test_production_refuses_a_fake_asr_backend() -> None:
    with pytest.raises(ValidationError, match="real ASR backend"):
        Settings(
            **BASE,
            APP_ENV="production",
            RADIO_QUEUE_BACKEND="sqs",
            RADIO_TRANSCRIPTION_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/t.fifo",
            RADIO_ANALYSIS_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/a.fifo",
            RADIO_ASR_BACKEND="fake",
        )


def test_s3_segment_store_requires_a_bucket() -> None:
    with pytest.raises(ValidationError, match="RADIO_S3_BUCKET is required"):
        Settings(
            RADIO_S3_BUCKET="",
            RADIO_AUDIO_TOKEN_SECRET="x" * 40,
            RADIO_QUEUE_BACKEND="memory",
            RADIO_SEGMENT_STORE="s3",
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("RADIO_SPEECH_CHUNK_SECONDS", -5, "between 1 and 3600"),
        ("RADIO_RING_BUFFER_SECONDS", 0, "between 1 and 3600"),
        ("RADIO_SQS_VISIBILITY_SECONDS", 0, "between 30 and 43200"),
        ("RADIO_SQS_VISIBILITY_SECONDS", 50_000, "between 30 and 43200"),
        ("RADIO_SQS_WAIT_TIME_SECONDS", 21, "between 0 and 20"),
        ("RADIO_SQS_MAX_MESSAGES_PER_RECEIVE", 11, "between 1 and 10"),
        ("RADIO_MAX_ACTIVE_UNIQUE_STATIONS", 100_000, "between 1 and 512"),
        ("RADIO_LISTENER_SHARD_COUNT", 0, "between 1 and 64"),
        ("RADIO_LISTENER_SHARD_INDEX", -1, "must not be negative"),
        ("RADIO_SAMPLE_RATE", 44_100, "must be 8000 or 16000"),
        ("RADIO_ASR_COMPUTE_TYPE", "int2", "must be one of"),
        ("RADIO_VAD_SPEECH_THRESHOLD", 1.5, "between 0.05 and 0.95"),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Settings(**BASE, **{field: value})


def test_shard_index_must_be_inside_shard_count() -> None:
    with pytest.raises(ValidationError, match="less than RADIO_LISTENER_SHARD_COUNT"):
        Settings(**BASE, RADIO_LISTENER_SHARD_COUNT=2, RADIO_LISTENER_SHARD_INDEX=2)


def test_preroll_must_fit_inside_the_ring_buffer() -> None:
    with pytest.raises(ValidationError, match="cannot exceed RADIO_RING_BUFFER_SECONDS"):
        Settings(**BASE, RADIO_RING_BUFFER_SECONDS=30, RADIO_PRE_KEYWORD_SECONDS=60)


def test_watermarks_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="warning < pause < emergency"):
        Settings(**BASE, RADIO_SPOOL_WARNING_PERCENT=90, RADIO_SPOOL_PAUSE_PERCENT=80)


def test_visibility_heartbeat_must_be_shorter_than_visibility() -> None:
    with pytest.raises(ValidationError, match="must be shorter than"):
        Settings(
            **BASE,
            RADIO_SQS_VISIBILITY_SECONDS=120,
            RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS=120,
        )


def test_listener_sessions_cannot_exceed_station_capacity() -> None:
    with pytest.raises(ValidationError, match="cannot exceed RADIO_MAX_ACTIVE_UNIQUE_STATIONS"):
        Settings(**BASE, RADIO_MAX_ACTIVE_UNIQUE_STATIONS=4, RADIO_LISTENER_MAX_SESSIONS=8)


def test_ffmpeg_binary_rejects_shell_metacharacters() -> None:
    """This value becomes argv[0]; a misconfigured env var must not smuggle args."""
    for hostile in ("ffmpeg; id", "ffmpeg $(id)", "ff mpeg", "ffmpeg|cat", ""):
        with pytest.raises(ValidationError, match="plain binary name"):
            Settings(**BASE, RADIO_LISTENER_FFMPEG_BINARY=hostile)


def test_ring_buffer_size_is_derived_not_asserted() -> None:
    """60 s x 16 kHz x 2 bytes = 1.92 MB per station; docs quote this property."""
    settings = Settings(**BASE)
    assert settings.ring_buffer_bytes_per_station == 60 * 16_000 * 2


def test_content_policy_defaults_exclude_song_lyrics() -> None:
    policy = Settings(**BASE).content_policy_defaults
    assert policy["include_song_lyrics"] is False
    assert policy["include_long_form_singing"] is False
    assert policy["include_advertisements"] is True
    assert policy["include_speech_over_music"] is True
    assert policy["include_sung_advertising_jingles"] is True


def test_capacity_default_is_conservative() -> None:
    """A regression guard against someone 'optimising' this to 1000."""
    settings = Settings(**BASE)
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 1
    assert settings.RADIO_MAX_REQUESTED_UNIQUE_STATIONS == 1000
    assert settings.RADIO_LISTENER_SHARD_COUNT == 1
    assert settings.RADIO_LISTENER_SHARD_INDEX == 0
