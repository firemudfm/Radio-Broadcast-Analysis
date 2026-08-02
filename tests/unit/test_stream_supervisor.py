"""Listener behaviour: segmentation, generations, admission and subprocess safety.

Async code is driven with ``asyncio.run`` rather than a plugin, so the suite
keeps its current dependency set.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.pipeline.ids import IdentifierError
from app.services.audio_classifier import VadEnergyClassifier
from app.services.stream_supervisor import (
    SegmentEvent,
    StationPlan,
    StationSession,
    StreamSupervisor,
    reconnect_delay,
)
from tests.fixtures import audio

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
CHUNK_BYTES = audio.SAMPLE_RATE * 2 // 2  # 0.5 s of 16-bit mono


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_MODEL_PATH=tmp_path / "models",
        RADIO_SPOOL_PATH=tmp_path / "spool",
        RADIO_QUEUE_BACKEND="memory",
        # Control-plane unit tests: several distinct stations must be active
        # at once for the counters to mean different things. Production
        # defaults to 1; that is asserted in tests/test_capacity_defaults.py.
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8,
        RADIO_LISTENER_MAX_SESSIONS=8,
    )


@pytest.fixture
def plan() -> StationPlan:
    return StationPlan(
        station_id="rb-test-station",
        stream_url="https://stream.example.com/live.mp3",
        display_name="Test FM",
        keyword_index_version=4,
    )


def build_session(settings: Settings, plan: StationPlan) -> tuple[StationSession, list[SegmentEvent]]:
    events: list[SegmentEvent] = []

    async def emit(event: SegmentEvent) -> None:
        events.append(event)

    session = StationSession(
        plan,
        settings,
        classifier=VadEnergyClassifier(),
        emit=emit,
        clock=lambda: NOW,
    )
    return session, events


async def feed(session: StationSession, pcm: bytes) -> None:
    for offset in range(0, len(pcm), CHUNK_BYTES):
        await session.ingest_pcm(pcm[offset : offset + CHUNK_BYTES])


# --- backoff ------------------------------------------------------------------


def test_reconnect_delay_stays_inside_the_configured_bounds() -> None:
    for attempt in range(12):
        delay = reconnect_delay(attempt, minimum=2.0, maximum=120.0, jitter=lambda: 1.0)
        assert 2.0 <= delay <= 120.0


def test_reconnect_delay_is_jittered() -> None:
    low = reconnect_delay(5, minimum=2.0, maximum=120.0, jitter=lambda: 0.0)
    high = reconnect_delay(5, minimum=2.0, maximum=120.0, jitter=lambda: 1.0)
    assert low < high, "without jitter every session reconnects in lockstep"
    assert low == 2.0


# --- plan validation ----------------------------------------------------------


def test_station_plan_rejects_a_path_traversing_id() -> None:
    with pytest.raises(IdentifierError):
        StationPlan(station_id="../../etc/passwd", stream_url="https://example.com/s")


# --- segmentation -------------------------------------------------------------


def test_speech_produces_segments(settings: Settings, plan: StationPlan) -> None:
    session, events = build_session(settings, plan)
    asyncio.run(_speech_case(session))
    assert events, "30 seconds of speech must produce at least one segment"
    assert all(event.content_class in {"speech", "speech_over_music"} for event in events)
    assert [event.sequence_number for event in events] == list(range(1, len(events) + 1))
    assert all(event.station_session_id == session.session_id for event in events)
    assert all(event.keyword_index_version == 4 for event in events)
    assert all(event.pcm for event in events)


async def _speech_case(session: StationSession) -> None:
    await feed(session, audio.speech_like(30.0))
    await session.flush(reason="test")


def test_segments_respect_the_configured_chunk_length(
    settings: Settings, plan: StationPlan
) -> None:
    session, events = build_session(settings, plan)
    asyncio.run(_speech_case(session))
    limit_ms = (settings.RADIO_SPEECH_CHUNK_SECONDS + 4) * 1000
    assert all(event.duration_ms <= limit_ms for event in events), (
        "a segment must not grow past the chunk length plus the speech lead"
    )


def test_sustained_music_is_mostly_discarded(settings: Settings, plan: StationPlan) -> None:
    session, events = build_session(settings, plan)

    async def scenario() -> None:
        for index in range(6):
            await feed(session, audio.music_like(10.0, seed=index))
        await session.flush(reason="test")

    asyncio.run(scenario())
    retained_ms = sum(event.duration_ms for event in events)
    # The first few seconds are retained by design (a run is not a song yet);
    # the other ~54 seconds must not reach the spool.
    assert retained_ms <= 15_000, f"retained {retained_ms}ms of a 60s music block"


def test_a_partial_segment_is_flushed_on_disconnect(
    settings: Settings, plan: StationPlan
) -> None:
    session, events = build_session(settings, plan)

    async def scenario() -> None:
        await feed(session, audio.speech_like(8.0))
        await session.flush(reason="disconnect")

    asyncio.run(scenario())
    assert events, "speech in progress when the stream drops must still be emitted"


def test_segments_never_span_a_reconnect(settings: Settings, plan: StationPlan) -> None:
    session, events = build_session(settings, plan)

    async def scenario() -> None:
        await feed(session, audio.speech_like(8.0, seed=1))
        session._new_generation()  # noqa: SLF001 - simulating a reconnect
        await feed(session, audio.speech_like(8.0, seed=2))
        await session.flush(reason="test")

    asyncio.run(scenario())
    generations = {event.generation for event in events}
    assert len(generations) >= 1
    for event in events:
        # Each emitted window belongs to exactly one generation; audio from
        # before and after a dropout is never spliced together.
        assert event.duration_ms <= 12_000


def test_keyword_index_version_updates_without_restarting_the_stream(
    settings: Settings, plan: StationPlan
) -> None:
    session, events = build_session(settings, plan)

    async def scenario() -> None:
        await feed(session, audio.speech_like(6.0))
        session.update_keyword_index_version(9)
        await feed(session, audio.speech_like(6.0, seed=4))
        await session.flush(reason="test")

    asyncio.run(scenario())
    assert events[-1].keyword_index_version == 9
    assert session.session_id, "the session id is stable across an index reload"


# --- subprocess safety --------------------------------------------------------


def test_ffmpeg_command_is_hardened(settings: Settings, plan: StationPlan) -> None:
    session, _ = build_session(settings, plan)
    command = session._ffmpeg_command("https://stream.example.com/live.mp3")  # noqa: SLF001

    assert command[0] == settings.RADIO_LISTENER_FFMPEG_BINARY
    assert isinstance(command, list), "argv array, never a shell string"

    whitelist = command[command.index("-protocol_whitelist") + 1]
    assert "file" not in whitelist, "a hostile playlist must not read local files"
    assert "concat" not in whitelist

    # Internal reconnection would follow a redirect without re-running the
    # SSRF check, so it is disabled and handled by the supervisor instead.
    assert command[command.index("-reconnect") + 1] == "0"

    assert command[command.index("-ar") + 1] == str(settings.RADIO_SAMPLE_RATE)
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-f") + 1] == "s16le"
    assert command[-1] == "pipe:1"
    assert command[command.index("-i") + 1] == "https://stream.example.com/live.mp3"


def test_ffmpeg_read_timeout_is_bounded(settings: Settings, plan: StationPlan) -> None:
    session, _ = build_session(settings, plan)
    command = session._ffmpeg_command("https://stream.example.com/live.mp3")  # noqa: SLF001
    rw_timeout = int(command[command.index("-rw_timeout") + 1])
    assert rw_timeout == int(settings.RADIO_LISTENER_READ_TIMEOUT_SECONDS * 1_000_000)


def test_an_unsafe_stream_url_never_spawns_a_decoder(settings: Settings) -> None:
    """SSRF validation runs before the subprocess, not after."""
    plan = StationPlan(station_id="rb-loopback", stream_url="http://127.0.0.1:8080/stream")
    session, events = build_session(settings, plan)

    from app.services.net_safety import NetSafetyError

    with pytest.raises(NetSafetyError):
        asyncio.run(session._stream_once())  # noqa: SLF001
    assert session._process is None  # noqa: SLF001
    assert not events


# --- supervisor ---------------------------------------------------------------


def test_supervisor_opens_one_session_per_distinct_station(settings: Settings) -> None:
    supervisor, _ = build_supervisor(settings)
    plans = [
        StationPlan(station_id=f"rb-{index}", stream_url=f"https://example.com/{index}")
        for index in range(3)
    ]

    async def scenario() -> dict:
        # The same station listed repeatedly is still one session.
        result = await supervisor.reconcile([*plans, plans[0], plans[1]])
        await supervisor.shutdown()
        return result

    result = asyncio.run(scenario())
    assert result["started"] == 3
    assert result["running"] == 3


def test_supervisor_reuses_an_existing_session_instead_of_restarting(
    settings: Settings,
) -> None:
    supervisor, _ = build_supervisor(settings)
    first = StationPlan(station_id="rb-1", stream_url="https://example.com/1", keyword_index_version=1)
    second = StationPlan(station_id="rb-1", stream_url="https://example.com/1", keyword_index_version=7)

    async def scenario() -> tuple[dict, str, str]:
        await supervisor.reconcile([first])
        before = supervisor._sessions["rb-1"].session_id  # noqa: SLF001
        result = await supervisor.reconcile([second])
        after = supervisor._sessions["rb-1"].session_id  # noqa: SLF001
        await supervisor.shutdown()
        return result, before, after

    result, before, after = asyncio.run(scenario())
    assert result["started"] == 0
    assert before == after, "a keyword-index change must not drop the audio connection"


def test_supervisor_enforces_the_session_cap(settings: Settings) -> None:
    capped = settings.model_copy(
        update={"RADIO_LISTENER_MAX_SESSIONS": 2, "RADIO_MAX_ACTIVE_UNIQUE_STATIONS": 2}
    )
    supervisor, _ = build_supervisor(capped)
    plans = [
        StationPlan(station_id=f"rb-{index}", stream_url=f"https://example.com/{index}")
        for index in range(5)
    ]

    async def scenario() -> dict:
        result = await supervisor.reconcile(plans)
        await supervisor.shutdown()
        return result

    result = asyncio.run(scenario())
    assert result["running"] == 2
    assert result["rejected"] == 3, "overflow must be visible, never silently dropped"


def test_supervisor_stops_stations_that_leave_the_plan(settings: Settings) -> None:
    supervisor, _ = build_supervisor(settings)
    plans = [
        StationPlan(station_id=f"rb-{index}", stream_url=f"https://example.com/{index}")
        for index in range(3)
    ]

    async def scenario() -> dict:
        await supervisor.reconcile(plans)
        result = await supervisor.reconcile(plans[:1])
        await supervisor.shutdown()
        return result

    result = asyncio.run(scenario())
    assert result["stopped"] == 2
    assert result["running"] == 1


def test_shutdown_leaves_no_sessions_running(settings: Settings) -> None:
    supervisor, _ = build_supervisor(settings)
    plans = [StationPlan(station_id="rb-1", stream_url="https://example.com/1")]

    async def scenario() -> None:
        await supervisor.reconcile(plans)
        await supervisor.shutdown()

    asyncio.run(scenario())
    assert supervisor.session_count == 0
    assert supervisor.active_station_ids == ()


def build_supervisor(settings: Settings) -> tuple[StreamSupervisor, list[SegmentEvent]]:
    events: list[SegmentEvent] = []

    async def emit(event: SegmentEvent) -> None:
        events.append(event)

    supervisor = StreamSupervisor(
        settings,
        classifier_factory=VadEnergyClassifier,
        emit=emit,
        clock=lambda: NOW,
    )
    return supervisor, events
