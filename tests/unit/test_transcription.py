"""Two-pass ASR policy, hallucination filtering and model-loading guards."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.pipeline.errors import ModelVerificationError, TranscriptionFailedError
from app.services.transcription import (
    NO_SPEECH_THRESHOLD,
    DecodeOptions,
    FakeTranscriptionEngine,
    FasterWhisperEngine,
    TranscriptionService,
    _build_result,
    build_engine,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_MODEL_PATH=tmp_path / "models",
        RADIO_ASR_BACKEND="fake",
    )


@pytest.fixture
def service(settings: Settings) -> TranscriptionService:
    engine = FakeTranscriptionEngine(responses={"*": "the new NVIDIA card"})
    return TranscriptionService(settings, engine=engine, confirmation_engine=engine)


AUDIO = b"\x01\x02" * 8_000


# --- two-pass policy ----------------------------------------------------------


def test_pass_a_is_cheap_and_pass_b_is_thorough(
    service: TranscriptionService, settings: Settings
) -> None:
    service.transcribe(AUDIO)
    service.confirm(AUDIO, language="en", prompt="NVIDIA")

    first, second = service.engine.calls
    assert first.asr_pass == "a"
    assert first.beam_size == settings.RADIO_ASR_BEAM_SIZE
    assert first.word_timestamps is False

    assert second.asr_pass == "b"
    assert second.beam_size == settings.RADIO_ASR_CONFIRMATION_BEAM_SIZE
    assert second.beam_size > first.beam_size
    assert second.word_timestamps is True, "evidence clips need word-level timings"
    assert second.initial_prompt == "NVIDIA"


def test_pass_b_emits_word_timestamps(service: TranscriptionService) -> None:
    result = service.confirm(AUDIO, language="en")
    assert result.words
    assert all(word.end_ms >= word.start_ms for word in result.words)
    assert result.asr_pass == "b"


def test_a_single_language_hint_pins_the_decoder(service: TranscriptionService) -> None:
    service.transcribe(AUDIO, language_hints=["hi"])
    assert service.engine.calls[-1].language == "hi"


def test_several_hints_leave_detection_on_for_code_switching(
    service: TranscriptionService,
) -> None:
    """Pinning one of several hints would discard the other language outright."""
    service.transcribe(AUDIO, language_hints=["hi", "en", "mr"])
    assert service.engine.calls[-1].language is None


def test_empty_hints_leave_detection_on(service: TranscriptionService) -> None:
    service.transcribe(AUDIO, language_hints=[])
    assert service.engine.calls[-1].language is None


def test_prompt_is_suppressed_when_the_budget_is_zero(settings: Settings) -> None:
    engine = FakeTranscriptionEngine(responses={"*": "hello"})
    tuned = settings.model_copy(update={"RADIO_ASR_PROMPT_MAX_CHARACTERS": 0})
    TranscriptionService(tuned, engine=engine, confirmation_engine=engine).confirm(
        AUDIO, prompt="NVIDIA, Amazon"
    )
    assert engine.calls[-1].initial_prompt is None


def test_the_original_language_transcript_is_preserved(service: TranscriptionService) -> None:
    result = service.transcribe(AUDIO)
    assert result.text == "the new NVIDIA card"
    assert result.language == "en"
    assert result.language_probability == pytest.approx(0.99)
    payload = result.as_payload()
    assert payload["model"] == "fake-whisper"
    assert payload["beam_size"] == result.beam_size
    assert "translated" not in payload, "nothing is translated by default"


def test_engines_are_shared_when_both_passes_name_one_model(settings: Settings) -> None:
    service = TranscriptionService(settings)
    assert service._confirmation_engine is service._engine  # noqa: SLF001


# --- result construction ------------------------------------------------------


class RawSegment:
    def __init__(self, text, start=0.0, end=1.0, no_speech_prob=0.0, avg_logprob=-0.2, words=None):
        self.text = text
        self.start = start
        self.end = end
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob
        self.words = words or []


def build(raw, **kwargs):
    defaults = {
        "language": "en",
        "language_probability": 0.9,
        "duration_ms": 20_000,
        "model_name": "m",
        "model_revision": None,
        "compute_type": "int8",
        "options": DecodeOptions(),
    }
    return _build_result(raw, **{**defaults, **kwargs})


def test_hallucinated_segments_on_non_speech_are_dropped() -> None:
    """A hallucinated brand name would otherwise become a false mention."""
    result = build(
        [
            RawSegment("real speech here", no_speech_prob=0.01),
            RawSegment("NVIDIA NVIDIA NVIDIA", no_speech_prob=NO_SPEECH_THRESHOLD + 0.05),
        ]
    )
    assert result.text == "real speech here"
    assert result.dropped_segments == 1


def test_low_confidence_segments_are_kept_but_flagged() -> None:
    result = build([RawSegment("faint speech", avg_logprob=-1.5)])
    assert result.text == "faint speech"
    assert result.segments[0].is_low_confidence


def test_blank_segments_are_ignored() -> None:
    result = build([RawSegment("   "), RawSegment("content")])
    assert result.text == "content"
    assert result.dropped_segments == 0


def test_timings_are_converted_to_milliseconds() -> None:
    result = build([RawSegment("hi", start=1.5, end=3.25)])
    assert result.segments[0].start_ms == 1500
    assert result.segments[0].end_ms == 3250


def test_empty_result_is_reported_as_empty() -> None:
    assert build([]).is_empty


# --- engine guards ------------------------------------------------------------


def test_a_missing_model_fails_with_a_named_error_not_a_silent_download(
    tmp_path: Path,
) -> None:
    # No `allow_download` argument exists any more: acquisition is an explicit
    # operator step, never a runtime fallback.
    engine = FasterWhisperEngine(
        model_name="Systran/faster-whisper-small",
        model_root=tmp_path / "models",
    )
    with pytest.raises((ModelVerificationError, Exception)) as error:
        engine.transcribe(AUDIO, DecodeOptions())
    # Either faster-whisper is absent (UnsupportedModelError) or the model is
    # (ModelVerificationError). Both are permanent and explicitly named.
    assert error.value.__class__.__name__ in {
        "ModelVerificationError",
        "UnsupportedModelError",
    }
    assert not error.value.retryable


def test_engine_failures_are_classified_retryable() -> None:
    engine = FakeTranscriptionEngine(failure=TranscriptionFailedError("engine blew up"))
    with pytest.raises(TranscriptionFailedError) as error:
        engine.transcribe(AUDIO, DecodeOptions())
    assert error.value.retryable


def test_build_engine_honours_the_fake_backend(settings: Settings) -> None:
    assert build_engine(settings).model_name == "fake-whisper"


def test_build_engine_selects_faster_whisper(settings: Settings) -> None:
    tuned = settings.model_copy(update={"RADIO_ASR_BACKEND": "faster_whisper"})
    engine = build_engine(tuned)
    assert isinstance(engine, FasterWhisperEngine)
    assert engine.compute_type == "int8"
    assert engine.model_name == tuned.RADIO_ASR_MODEL
