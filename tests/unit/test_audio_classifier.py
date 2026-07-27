"""Audio classification and the keep/discard policy (ADR-005, ADR-010).

The assertions that matter here are about *retention*, not about labels. A
wrong label costs a transcription; a wrong discard loses a mention permanently,
so the discard paths are tested far more strictly than the keep paths.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.pipeline.errors import ClassifierUnavailableError
from app.services.audio_classifier import (
    PassthroughClassifier,
    RollingAudioPolicy,
    SileroVad,
    VadEnergyClassifier,
    YamnetClassifier,
    analyse_window,
    build_classifier,
)
from app.services.ring_buffer import PcmWindow
from tests.fixtures import audio

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
WINDOW_SECONDS = 3.0


def window(pcm: bytes, offset_ms: int = 0) -> PcmWindow:
    return PcmWindow(
        pcm=pcm,
        sample_rate=audio.SAMPLE_RATE,
        start_offset_ms=offset_ms,
        started_at=NOW,
        ended_at=NOW,
        generation=1,
    )


@pytest.fixture
def classifier() -> VadEnergyClassifier:
    return VadEnergyClassifier()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_MODEL_PATH=tmp_path / "models",
    )


# --- per-window classification ------------------------------------------------


# Factories rather than bytes: parametrising on the PCM itself puts megabytes of
# audio into the test id, which pytest exports via PYTEST_CURRENT_TEST.
@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (audio.silence, "silence"),
        (audio.music_like, "music"),
        (audio.speech_like, "speech"),
        (audio.speech_over_music, "speech_over_music"),
    ],
    ids=["silence", "instrumental", "clear-speech", "advertisement"],
)
def test_classifies_the_four_canonical_cases(
    classifier: VadEnergyClassifier, factory, expected: str
) -> None:
    assert classifier.classify(window(factory(WINDOW_SECONDS))).content_class == expected


def test_speech_survives_a_wide_range_of_music_bed_levels(
    classifier: VadEnergyClassifier,
) -> None:
    """A spoken ad must not be labelled ``singing`` just because the bed is loud.

    ``singing`` is a discard candidate, so this is the difference between
    catching an advertisement and deleting it.
    """
    for bed in (2000, 3200, 4800, 6400):
        pcm = audio.mix(
            audio.speech_like(WINDOW_SECONDS, amplitude=8000, seed=5),
            audio.music_like(WINDOW_SECONDS, amplitude=bed, seed=6),
        )
        result = classifier.classify(window(pcm))
        assert result.content_class in {"speech", "speech_over_music"}, (
            f"bed={bed} produced {result.content_class}, which risks discarding a spoken ad"
        )


def test_a_window_too_short_to_measure_is_unknown_not_music(
    classifier: VadEnergyClassifier,
) -> None:
    result = classifier.classify(window(b"\x00\x00" * 10))
    assert result.content_class == "unknown"
    assert not result.is_discardable


def test_signals_are_reported_for_every_verdict(classifier: VadEnergyClassifier) -> None:
    result = classifier.classify(window(audio.speech_like(WINDOW_SECONDS)))
    assert {"low_energy_ratio", "zcr_variance", "energy_variance"} <= set(result.signals)
    assert result.reason


def test_speech_and_music_signals_are_actually_separated() -> None:
    speech = analyse_window(window(audio.speech_like(WINDOW_SECONDS)))
    music = analyse_window(window(audio.music_like(WINDOW_SECONDS)))
    assert speech.low_energy_ratio > music.low_energy_ratio
    assert speech.zcr_variance > music.zcr_variance
    assert speech.energy_variance > music.energy_variance


# --- rolling policy -----------------------------------------------------------


def _run(policy: RollingAudioPolicy, classifier, tracks: list[bytes]) -> list[bool]:
    offset = 0
    decisions = []
    for pcm in tracks:
        decision = policy.observe(classifier.classify(window(pcm, offset)))
        decisions.append(decision.keep)
        offset += int(WINDOW_SECONDS * 1000)
    return decisions


def test_music_is_never_discarded_on_a_single_window(
    settings: Settings, classifier: VadEnergyClassifier
) -> None:
    policy = RollingAudioPolicy(settings)
    kept = _run(policy, classifier, [audio.music_like(WINDOW_SECONDS, seed=1)])
    assert kept == [True], "one window of music is not evidence of a song"


def test_sustained_music_is_discarded_after_the_configured_threshold(
    settings: Settings, classifier: VadEnergyClassifier
) -> None:
    policy = RollingAudioPolicy(settings)
    kept = _run(
        policy, classifier, [audio.music_like(WINDOW_SECONDS, seed=index) for index in range(6)]
    )
    # 8-second threshold, 3-second windows: retained through 6s, dropped at 9s.
    assert kept[:2] == [True, True]
    assert kept[2:] == [False, False, False, False]


def test_a_long_song_is_discarded_even_when_it_follows_speech(
    settings: Settings, classifier: VadEnergyClassifier
) -> None:
    """The jingle allowance must be bounded, or songs are never discarded.

    Radio plays speech before nearly every track, so an unbounded
    "adjacent to speech" exemption would retain every song on the station.
    """
    policy = RollingAudioPolicy(settings)
    tracks = [audio.speech_like(WINDOW_SECONDS)]
    tracks += [audio.music_like(WINDOW_SECONDS, seed=index) for index in range(14)]
    kept = _run(policy, classifier, tracks)
    assert kept[0] is True
    assert kept[-1] is False, "a 42-second musical run after speech is a song, not a jingle"
    retained_seconds = sum(kept[1:]) * WINDOW_SECONDS
    assert retained_seconds <= settings.RADIO_JINGLE_MAX_SECONDS + WINDOW_SECONDS


def test_a_short_jingle_between_speech_is_retained(
    settings: Settings, classifier: VadEnergyClassifier
) -> None:
    policy = RollingAudioPolicy(settings)
    tracks = [
        audio.speech_like(WINDOW_SECONDS, seed=1),
        audio.music_like(WINDOW_SECONDS, seed=2),
        audio.music_like(WINDOW_SECONDS, seed=3),
        audio.speech_like(WINDOW_SECONDS, seed=4),
    ]
    assert all(_run(policy, classifier, tracks))


def test_jingle_allowance_is_disabled_by_policy(classifier: VadEnergyClassifier, tmp_path) -> None:
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_INCLUDE_SUNG_ADVERTISING_JINGLES=False,
    )
    policy = RollingAudioPolicy(settings)
    tracks = [audio.speech_like(WINDOW_SECONDS)]
    tracks += [audio.music_like(WINDOW_SECONDS, seed=index) for index in range(5)]
    kept = _run(policy, classifier, tracks)
    assert kept[-1] is False


def test_short_silence_is_kept_because_it_may_be_a_pause(
    settings: Settings, classifier: VadEnergyClassifier
) -> None:
    policy = RollingAudioPolicy(settings)
    kept = _run(policy, classifier, [audio.silence(WINDOW_SECONDS)] * 3)
    assert all(kept), "9s < RADIO_SILENCE_END_SECONDS: this may be a pause mid-sentence"


def test_sustained_silence_ends_retention(
    settings: Settings, classifier: VadEnergyClassifier
) -> None:
    policy = RollingAudioPolicy(settings)
    kept = _run(policy, classifier, [audio.silence(WINDOW_SECONDS)] * 6)
    assert kept[-1] is False


def test_uncertain_audio_is_retained_by_default(settings: Settings) -> None:
    class UncertainClassifier:
        name = "uncertain"

        def classify(self, window_):
            from app.services.audio_classifier import ClassificationResult

            return ClassificationResult(
                content_class="unknown",
                confidence=0.2,
                signals={},
                reason="test",
                duration_ms=3000,
            )

        def reset(self) -> None:
            return None

    policy = RollingAudioPolicy(settings)
    kept = _run(policy, UncertainClassifier(), [audio.silence(WINDOW_SECONDS)] * 10)
    assert all(kept), "recall is protected in stage 1; precision is recovered from the transcript"


def test_uncertain_audio_can_be_dropped_when_explicitly_configured() -> None:
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_TRANSCRIBE_UNCERTAIN_AUDIO=False,
    )
    policy = RollingAudioPolicy(settings)

    from app.services.audio_classifier import ClassificationResult

    decision = policy.observe(
        ClassificationResult(
            content_class="unknown", confidence=0.2, signals={}, reason="t", duration_ms=3000
        )
    )
    assert decision.keep is False


def test_speech_over_music_can_be_excluded_by_policy() -> None:
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_INCLUDE_SPEECH_OVER_MUSIC=False,
    )
    policy = RollingAudioPolicy(settings)

    from app.services.audio_classifier import ClassificationResult

    decision = policy.observe(
        ClassificationResult(
            content_class="speech_over_music",
            confidence=0.8,
            signals={},
            reason="t",
            duration_ms=3000,
        )
    )
    assert decision.keep is False


def test_reset_clears_rolling_state(settings: Settings, classifier: VadEnergyClassifier) -> None:
    policy = RollingAudioPolicy(settings)
    _run(policy, classifier, [audio.music_like(WINDOW_SECONDS, seed=i) for i in range(6)])
    policy.reset()
    assert policy.current_run_ms == 0
    kept = _run(policy, classifier, [audio.music_like(WINDOW_SECONDS, seed=9)])
    assert kept == [True], "a reconnect must not inherit the previous stream's run"


# --- backends -----------------------------------------------------------------


def test_passthrough_transcribes_everything() -> None:
    result = PassthroughClassifier().classify(window(audio.music_like(WINDOW_SECONDS)))
    assert result.content_class == "speech"


def test_yamnet_refuses_to_start_rather_than_substituting_a_model() -> None:
    with pytest.raises(ClassifierUnavailableError) as error:
        YamnetClassifier()
    assert "vad_energy" in str(error.value.detail)


def test_build_classifier_selects_the_configured_backend(settings: Settings) -> None:
    assert build_classifier(settings).name == "vad_energy"


def test_build_classifier_honours_passthrough(tmp_path) -> None:
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_AUDIO_CLASSIFIER="passthrough",
    )
    assert build_classifier(settings).name == "passthrough"


def test_missing_silero_model_degrades_instead_of_raising(tmp_path) -> None:
    vad = SileroVad(tmp_path / "absent" / "silero_vad.onnx")
    assert vad.available is False
    assert vad.speech_probability(window(audio.speech_like(1.0))) is None


def test_classifier_works_without_vad(settings: Settings) -> None:
    """The listener must run on a host with no ONNX runtime and no model."""
    classifier = build_classifier(settings)
    assert classifier.classify(window(audio.speech_like(WINDOW_SECONDS))).content_class == "speech"
