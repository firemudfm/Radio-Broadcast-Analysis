"""Content typing and campaign content policy (ADR-010 §3)."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.content_classifier import (
    PassthroughContentClassifier,
    RulesContentClassifier,
    build_content_classifier,
    is_included,
)


@pytest.fixture
def classifier() -> RulesContentClassifier:
    return RulesContentClassifier()


# --- the worked examples from the requirements --------------------------------


def test_spoken_advertisement_over_music_is_an_advertisement(
    classifier: RulesContentClassifier,
) -> None:
    decision = classifier.classify(
        "Call now for 20% off the new NVIDIA laptop, available at all stores",
        audio_class="speech_over_music",
        duration_ms=15_000,
    )
    assert decision.content_type == "advertisement"
    included, reason = is_included(decision.content_type, {"include_advertisements": True})
    assert included and reason is None


def test_a_song_mentioning_a_brand_is_excluded_by_default(
    classifier: RulesContentClassifier,
) -> None:
    """The canonical false positive this pipeline must not produce."""
    decision = classifier.classify(
        "Amazon river flowing Amazon river flowing down the Amazon river flowing",
        audio_class="singing",
        duration_ms=30_000,
    )
    assert decision.content_type == "song_lyrics"
    included, reason = is_included("song_lyrics", {"include_song_lyrics": False})
    assert not included
    assert reason and "include_song_lyrics" in reason


def test_an_emergency_announcement_is_recognised(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify(
        "This is an emergency. Please evacuate the area immediately. This is not a test.",
        audio_class="speech",
        duration_ms=20_000,
    )
    assert decision.content_type == "emergency_alert"
    assert decision.confidence >= 0.6
    assert is_included("emergency_alert", {"include_emergency_alerts": True})[0]


# --- cue matching -------------------------------------------------------------


def test_short_cues_do_not_match_inside_longer_words(
    classifier: RulesContentClassifier,
) -> None:
    """"am" inside "Amazon" once labelled a song as a station ident."""
    decision = classifier.classify(
        "Amazon and Amazonia and amazement everywhere in the Amazon basin today",
        audio_class="speech",
        duration_ms=20_000,
    )
    assert decision.content_type != "station_identification"


def test_news_cues_are_recognised(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify(
        "According to officials, the government said today that police reported a rise.",
        audio_class="speech",
        duration_ms=30_000,
    )
    assert decision.content_type == "news"


def test_a_short_ident_is_recognised(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify("You're listening to 98.3 FM", duration_ms=4_000)
    assert decision.content_type == "station_identification"


def test_an_ident_cue_inside_long_speech_is_not_an_ident(
    classifier: RulesContentClassifier,
) -> None:
    decision = classifier.classify(
        "this is a very long discussion about many different subjects " * 4,
        audio_class="speech",
        duration_ms=120_000,
    )
    assert decision.content_type != "station_identification"


def test_interview_cues(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify(
        "Thank you for joining us. My guest today will tell us about the launch.",
        audio_class="speech",
        duration_ms=60_000,
    )
    assert decision.content_type == "interview"


# --- multilingual -------------------------------------------------------------


def test_hindi_advertisement_cues(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify(
        "अभी खरीदें! भारी छूट, मुफ्त डिलीवरी उपलब्ध है।",
        audio_class="speech_over_music",
        duration_ms=15_000,
    )
    assert decision.content_type == "advertisement"


def test_hindi_emergency_cues(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify(
        "आपातकाल की चेतावनी, कृपया सुरक्षित स्थान पर जाएं",
        duration_ms=12_000,
    )
    assert decision.content_type == "emergency_alert"


def test_an_unevaluated_language_falls_through_to_unknown_and_stays_included(
    classifier: RulesContentClassifier,
) -> None:
    """Guessing on a language with no evaluated cues would drop real mentions."""
    decision = classifier.classify(
        "Guten Tag, hier sind die Nachrichten aus Berlin und Umgebung heute Abend",
        audio_class="speech",
        duration_ms=20_000,
    )
    assert decision.content_type == "unknown"
    assert decision.policy_flag is None
    assert is_included("unknown", {"include_song_lyrics": False})[0]


# --- sung content -------------------------------------------------------------


def test_a_sung_advertising_jingle_is_an_advertisement_not_a_song(
    classifier: RulesContentClassifier,
) -> None:
    decision = classifier.classify(
        "Buy now at Amazon, limited time offer, free shipping today",
        audio_class="singing",
        duration_ms=12_000,
    )
    assert decision.content_type == "advertisement"


def test_sung_content_without_advertising_vocabulary_is_lyrics(
    classifier: RulesContentClassifier,
) -> None:
    decision = classifier.classify(
        "walking down the road tonight under the stars again",
        audio_class="singing",
        duration_ms=25_000,
    )
    assert decision.content_type == "song_lyrics"


def test_repetition_raises_confidence_in_a_lyric_verdict(
    classifier: RulesContentClassifier,
) -> None:
    repetitive = classifier.classify(
        "la la la love you la la la love you la la la love you baby",
        audio_class="singing",
        duration_ms=25_000,
    )
    varied = classifier.classify(
        "walking down the road tonight under distant stars again",
        audio_class="singing",
        duration_ms=25_000,
    )
    assert repetitive.confidence > varied.confidence


# --- degenerate input ---------------------------------------------------------


def test_empty_transcript_is_unknown(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify("", duration_ms=0)
    assert decision.content_type == "unknown"
    assert decision.confidence == 0.0


def test_a_very_short_transcript_is_not_guessed_at(
    classifier: RulesContentClassifier,
) -> None:
    decision = classifier.classify("ok then", audio_class="speech", duration_ms=2_000)
    assert decision.content_type == "unknown"


def test_signals_are_reported(classifier: RulesContentClassifier) -> None:
    decision = classifier.classify(
        "According to officials the government reported a rise", duration_ms=20_000
    )
    assert decision.signals["word_count"] > 0
    assert "distinct_word_ratio" in decision.signals


# --- policy and construction --------------------------------------------------


def test_types_without_a_policy_flag_are_always_included() -> None:
    assert is_included("discussion", {})[0]
    assert is_included("unknown", {})[0]


def test_a_disabled_flag_excludes_with_a_reason() -> None:
    included, reason = is_included("advertisement", {"include_advertisements": False})
    assert not included
    assert reason == "include_advertisements is disabled for this campaign"


def test_passthrough_classifier_labels_everything_unknown() -> None:
    decision = PassthroughContentClassifier().classify("anything at all")
    assert decision.content_type == "unknown"
    assert is_included(decision.content_type, {})[0]


def test_build_selects_the_configured_backend() -> None:
    base = {"RADIO_S3_BUCKET": "b", "RADIO_AUDIO_TOKEN_SECRET": "x" * 48}
    assert build_content_classifier(Settings(**base)).name == "rules"
    assert (
        build_content_classifier(
            Settings(**base, RADIO_CONTENT_CLASSIFIER="passthrough")
        ).name
        == "passthrough"
    )
