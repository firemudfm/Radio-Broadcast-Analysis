"""Conversation assembly: pre-roll, close conditions and ordering guards."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.services.conversation_assembler import (
    ConversationAssembler,
    TranscribedSegment,
)
from app.services.keyword_matcher import KeywordMatch

START = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
STATION = "rb-station"
SESSION = "session-1"


@pytest.fixture
def settings() -> Settings:
    return Settings(RADIO_S3_BUCKET="b", RADIO_AUDIO_TOKEN_SECRET="x" * 48)


@pytest.fixture
def assembler(settings: Settings) -> ConversationAssembler:
    return ConversationAssembler(settings)


def match(keyword_id: str = "kw-1", *campaign_ids: str, level: str = "exact") -> KeywordMatch:
    return KeywordMatch(
        keyword_id=keyword_id,
        campaign_ids=campaign_ids or ("campaign-a",),
        canonical_value="NVIDIA",
        matched_text="NVIDIA",
        match_level=level,  # type: ignore[arg-type]
        start_char=0,
        end_char=6,
        confidence=1.0,
    )


def segment(
    sequence: int,
    *,
    text: str = "some speech",
    at_second: float | None = None,
    duration: float = 20.0,
    matches: tuple[KeywordMatch, ...] = (),
    session: str = SESSION,
    content_class: str = "speech",
    language: str | None = "en",
) -> TranscribedSegment:
    offset = sequence * duration if at_second is None else at_second
    started = START + timedelta(seconds=offset)
    return TranscribedSegment(
        segment_id=f"segment-{sequence}",
        station_id=STATION,
        station_session_id=session,
        sequence_number=sequence,
        transcript_id=f"transcript-{sequence}",
        text=text,
        started_at=started,
        ended_at=started + timedelta(seconds=duration),
        duration_ms=int(duration * 1000),
        content_class=content_class,
        language=language,
        matches=matches,
    )


# --- opening and pre-roll -----------------------------------------------------


def test_no_conversation_without_a_keyword(assembler: ConversationAssembler) -> None:
    for index in range(5):
        assert assembler.observe(segment(index)) == []
    assert assembler.close(STATION, reason="shutdown") is None, (
        "speech nobody is tracking is not a mention"
    )


def test_a_match_opens_a_conversation_with_pre_keyword_context(
    assembler: ConversationAssembler, settings: Settings
) -> None:
    assembler.observe(segment(0, text="Earlier in the show"))
    assembler.observe(segment(1, text="we discussed graphics cards"))
    assembler.observe(segment(2, text="the new NVIDIA launch", matches=(match(),)))
    closed = assembler.close(STATION, reason="silence")

    assert closed is not None
    assert "graphics cards" in closed.transcript_text, "the lead-in must be included"
    assert "the new NVIDIA launch" in closed.transcript_text
    assert closed.first_sequence_number < 2


def test_pre_roll_is_bounded_by_the_configured_window(settings: Settings) -> None:
    assembler = ConversationAssembler(settings)
    # Segments 0..4 are 20s each; the keyword lands at t=100s and the window is
    # RADIO_PRE_KEYWORD_SECONDS (30s), so only the immediately preceding speech
    # may be pulled in.
    for index in range(5):
        assembler.observe(segment(index, text=f"old context {index}"))
    assembler.observe(segment(5, text="NVIDIA now", matches=(match(),)))
    closed = assembler.close(STATION, reason="silence")

    assert closed is not None
    assert "old context 0" not in closed.transcript_text
    assert "old context 4" in closed.transcript_text


def test_a_conversation_keeps_collecting_after_the_keyword(
    assembler: ConversationAssembler,
) -> None:
    assembler.observe(segment(0, text="NVIDIA announced", matches=(match(),)))
    assembler.observe(segment(1, text="and the price is competitive"))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert "price is competitive" in closed.transcript_text


# --- one conversation, many campaigns -----------------------------------------


def test_one_conversation_maps_to_every_matching_campaign(
    assembler: ConversationAssembler,
) -> None:
    """The core promise: transcribe once, analyse once, attribute many times."""
    assembler.observe(
        segment(
            0,
            text="NVIDIA and Amazon both announced",
            matches=(
                match("kw-nvidia", "campaign-a", "campaign-b"),
                match("kw-amazon", "campaign-b", "campaign-c"),
            ),
        )
    )
    closed = assembler.close(STATION, reason="silence")

    assert closed is not None
    assert closed.campaign_ids == ("campaign-a", "campaign-b", "campaign-c")
    assert closed.keyword_ids == ("kw-amazon", "kw-nvidia")


def test_a_keyword_repeated_is_still_one_mention(assembler: ConversationAssembler) -> None:
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),)))
    assembler.observe(segment(1, text="NVIDIA again", matches=(match(),)))
    assembler.observe(segment(2, text="NVIDIA once more", matches=(match(),)))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert len(closed.matches) == 1, "a brand said three times is one mention of it"


# --- close conditions ---------------------------------------------------------


def test_a_long_silence_gap_closes_the_conversation(
    assembler: ConversationAssembler, settings: Settings
) -> None:
    assembler.observe(segment(0, text="NVIDIA news", matches=(match(),), duration=20.0))
    gap = settings.RADIO_SILENCE_END_SECONDS + 5
    closed = assembler.observe(segment(1, at_second=20.0 + gap, text="unrelated later talk"))
    assert len(closed) == 1
    assert closed[0].close_reason == "silence"
    assert "unrelated later talk" not in closed[0].transcript_text


def test_a_music_boundary_closes_with_the_music_reason(
    assembler: ConversationAssembler, settings: Settings
) -> None:
    assembler.observe(segment(0, text="NVIDIA news", matches=(match(),)))
    gap = settings.RADIO_SILENCE_END_SECONDS + 5
    closed = assembler.observe(
        segment(1, at_second=20.0 + gap, text="la la la", content_class="singing")
    )
    assert closed[0].close_reason == "music"


def test_maximum_duration_closes_a_runaway_conversation(settings: Settings) -> None:
    assembler = ConversationAssembler(settings)
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),), duration=20.0))
    closed: list = []
    for index in range(1, 30):
        closed.extend(assembler.observe(segment(index, duration=20.0)))
        if closed:
            break
    assert closed, "a conversation must not grow without bound"
    assert closed[0].close_reason == "max_duration"
    assert closed[0].duration_ms <= (settings.RADIO_MAX_CONVERSATION_SECONDS + 40) * 1000


def test_a_reconnect_closes_the_conversation(assembler: ConversationAssembler) -> None:
    assembler.observe(segment(0, text="NVIDIA news", matches=(match(),)))
    closed = assembler.observe(segment(1, session="session-2", text="after reconnect"))
    assert len(closed) == 1
    assert closed[0].close_reason == "disconnect"
    assert "after reconnect" not in closed[0].transcript_text


def test_close_all_drains_every_station(settings: Settings) -> None:
    assembler = ConversationAssembler(settings)
    for station in ("rb-a", "rb-b"):
        assembler.observe(
            TranscribedSegment(
                segment_id=f"segment-{station}",
                station_id=station,
                station_session_id=SESSION,
                sequence_number=1,
                transcript_id=f"transcript-{station}",
                text="NVIDIA",
                started_at=START,
                ended_at=START + timedelta(seconds=20),
                duration_ms=20_000,
                matches=(match(),),
            )
        )
    closed = assembler.close_all(reason="shutdown")
    assert {item.station_id for item in closed} == {"rb-a", "rb-b"}
    assert all(item.close_reason == "shutdown" for item in closed)


def test_closing_is_idempotent(assembler: ConversationAssembler) -> None:
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),)))
    assert assembler.close(STATION, reason="silence") is not None
    assert assembler.close(STATION, reason="silence") is None, "no duplicate analysis job"


# --- ordering guards ----------------------------------------------------------


def test_duplicate_segments_are_ignored(assembler: ConversationAssembler) -> None:
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),)))
    assembler.observe(segment(1, text="follow up"))
    assembler.observe(segment(1, text="follow up"))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert closed.transcript_text.count("follow up") == 1


def test_out_of_order_segments_are_rejected_not_reordered(
    assembler: ConversationAssembler,
) -> None:
    # Contiguous in time so the conversation stays open; only the *sequence*
    # numbers are out of order.
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),), at_second=0, duration=20))
    assembler.observe(segment(5, text="continues here", at_second=20, duration=20))
    assembler.observe(segment(2, text="stale arrival", at_second=40, duration=20))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert "continues here" in closed.transcript_text
    assert "stale arrival" not in closed.transcript_text, (
        "reordering would produce a transcript that never happened"
    )


def test_sequence_gaps_are_recorded_but_do_not_end_a_conversation(
    assembler: ConversationAssembler,
) -> None:
    """Gaps are normal: discarded music consumes sequence numbers."""
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),), at_second=0, duration=20))
    assembler.observe(segment(3, text="still talking", at_second=20, duration=20))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert closed.missing_sequences == (1, 2)
    assert "still talking" in closed.transcript_text


# --- metadata -----------------------------------------------------------------


def test_dominant_language_is_weighted_by_transcript_length(
    assembler: ConversationAssembler,
) -> None:
    assembler.observe(segment(0, text="short", language="en", matches=(match(),)))
    assembler.observe(segment(1, text="a considerably longer stretch of hindi speech", language="hi"))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert closed.detected_language == "hi"


def test_candidate_only_conversations_are_flagged_for_confirmation(
    assembler: ConversationAssembler,
) -> None:
    assembler.observe(segment(0, text="volkswagon", matches=(match(level="fuzzy"),)))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert closed.requires_confirmation, "a fuzzy-only conversation needs pass B"


def test_a_confirmed_match_does_not_require_confirmation(
    assembler: ConversationAssembler,
) -> None:
    assembler.observe(segment(0, text="NVIDIA", matches=(match(),)))
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    assert not closed.requires_confirmation


def test_state_transitions_are_observable(assembler: ConversationAssembler) -> None:
    assert assembler.state_of(STATION) == "idle"
    assembler.observe(segment(0, text="context"))
    assert assembler.state_of(STATION) == "idle"
    assembler.observe(segment(1, text="NVIDIA", matches=(match(),)))
    assert assembler.state_of(STATION) == "open"
    assert assembler.open_conversation_ids()
    assembler.close(STATION, reason="silence")
    assert assembler.state_of(STATION) == "idle"
    assert assembler.open_conversation_ids() == ()
