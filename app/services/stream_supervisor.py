"""Shared multi-station listener (ADR-008 §2).

**One async session per DISTINCT station.** Not one container per station, not
one process per campaign, not one worker per keyword. A station referenced by
fifty campaigns is opened exactly once, and every campaign reads the same
decoded audio.

Each session owns:

* an FFmpeg subprocess that decodes whatever the station is serving (MP3, AAC,
  HLS, Ogg) into 16 kHz mono s16le;
* a bounded ring buffer (:mod:`app.services.ring_buffer`);
* a rolling audio policy (:mod:`app.services.audio_classifier`);
* a monotonic per-station sequence number and a session generation id.

Security posture
----------------
Stream URLs come from Radio Browser and are untrusted.

* Every connection attempt re-validates the URL with
  :func:`app.services.net_safety.validate_public_http_url` -- not once at
  configuration time, because DNS can be re-pointed at a private address
  between then and now.
* FFmpeg's own ``-reconnect`` is deliberately **off**. Letting FFmpeg reconnect
  internally would let it follow a redirect to a new host without our
  re-validating it. Reconnection is handled here so the SSRF check runs again
  on every attempt.
* ``-protocol_whitelist`` excludes ``file`` and ``concat``, so a hostile
  playlist cannot make the decoder read local files.
* Subprocesses are spawned with explicit argument arrays. ``shell=True`` never
  appears, and both configurable values that reach argv (the binary name and
  the bitrate) are pattern-validated in :mod:`app.config`.
* Termination escalates SIGTERM -> SIGKILL with a bounded wait, and every exit
  is awaited, so a stopped station cannot leave a zombie decoder holding a
  socket.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from ..observability import log_fields, trace_context
from ..pipeline.enums import AudioContentClass
from ..pipeline.ids import new_id, validate_station_id
from .audio_classifier import AudioClassifier, RollingAudioPolicy
from .net_safety import NetSafetyError, validate_public_http_url
from .ring_buffer import RingBuffer, bytes_per_second

logger = logging.getLogger(__name__)

#: How much audio to pull from the decoder per read. Small enough that a
#: shutdown is responsive, large enough not to spin the event loop.
READ_CHUNK_SECONDS = 0.5

#: Extra audio prepended to the first segment of a speech run, compensating for
#: the classifier needing a full window before it can call something speech.
#: Without it the first word of every conversation is clipped.
SPEECH_LEAD_SECONDS = 1.0
#: Sorts before any real timestamp, so a never-served station is first in
#: line for a listening turn.
_EPOCH = datetime.min.replace(tzinfo=UTC)

#: Grace period between SIGTERM and SIGKILL for a decoder subprocess.
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class StationPlan:
    """A station this listener has been assigned."""

    station_id: str
    stream_url: str
    display_name: str = ""
    keyword_index_version: int = 0
    language_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_station_id(self.station_id)


@dataclass(frozen=True)
class SegmentEvent:
    """One retained stretch of audio, ready to be stored and queued."""

    station_id: str
    station_session_id: str
    sequence_number: int
    pcm: bytes
    sample_rate: int
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    content_class: AudioContentClass
    content_class_confidence: float
    classifier_signals: dict[str, float]
    keyword_index_version: int
    trace_id: str
    generation: int


@dataclass
class SessionStatus:
    """Live health of one station session, surfaced through the API."""

    station_id: str
    station_session_id: str
    generation: int
    status: str = "connecting"
    last_audio_at_utc: datetime | None = None
    last_error: str | None = None
    codec: str | None = None
    sample_rate: int | None = None
    bitrate_kbps: int | None = None
    segments_emitted: int = 0
    bytes_decoded: int = 0
    reconnects: int = 0
    started_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: When speech was last heard. Drives rotation dwell: a station that is
    #: still talking keeps its slot instead of being cut mid-sentence.
    last_speech_at_utc: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "station_session_id": self.station_session_id,
            "generation": self.generation,
            "status": self.status,
            "last_audio_at_utc": self.last_audio_at_utc.isoformat() if self.last_audio_at_utc else None,
            "last_error": self.last_error,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "bitrate_kbps": self.bitrate_kbps,
            "last_speech_at_utc": (
                self.last_speech_at_utc.isoformat() if self.last_speech_at_utc else None
            ),
            "segments_emitted": self.segments_emitted,
            "bytes_decoded": self.bytes_decoded,
            "reconnects": self.reconnects,
            "started_at_utc": self.started_at_utc.isoformat(),
        }


SegmentSink = Callable[[SegmentEvent], Awaitable[None]]
StatusSink = Callable[[SessionStatus], Awaitable[None]]


def reconnect_delay(
    attempt: int,
    *,
    minimum: float,
    maximum: float,
    jitter: Callable[[], float] | None = None,
) -> float:
    """Exponential backoff with full jitter, capped at ``maximum``.

    Full jitter rather than a fixed schedule: when a shared upstream (a CDN, a
    stream host serving many stations) fails, every session backs off on the
    same clock and would otherwise reconnect in lockstep and re-create the
    overload.
    """
    base = min(maximum, minimum * (2 ** max(0, attempt)))
    return minimum + (base - minimum) * (jitter or random.random)()


class StationSession:
    """Supervises one station: connect, decode, classify, emit, reconnect."""

    def __init__(
        self,
        plan: StationPlan,
        settings: Settings,
        *,
        classifier: AudioClassifier,
        emit: SegmentSink,
        on_status: StatusSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._plan = plan
        self._settings = settings
        self._classifier = classifier
        self._emit = emit
        self._on_status = on_status
        self._clock = clock or (lambda: datetime.now(UTC))

        self._policy = RollingAudioPolicy(settings)
        self._buffer = RingBuffer(
            sample_rate=settings.RADIO_SAMPLE_RATE,
            seconds=settings.RADIO_RING_BUFFER_SECONDS,
            started_at=self._clock(),
        )
        self._session_id = new_id()
        self._sequence = 0
        self._stop = asyncio.Event()
        self._process: asyncio.subprocess.Process | None = None
        self._status = SessionStatus(
            station_id=plan.station_id,
            station_session_id=self._session_id,
            generation=self._buffer.generation,
            sample_rate=settings.RADIO_SAMPLE_RATE,
            # From the injected clock, not wall time: rotation measures a turn
            # against this, so both must come from the same source of time.
            started_at_utc=self._clock(),
        )

        # Segment accumulation state.
        self._segment_open = False
        self._segment_start_ms = 0
        self._segment_end_ms = 0
        self._segment_class: AudioContentClass = "unknown"
        self._segment_confidence = 0.0
        self._segment_signals: dict[str, float] = {}
        self._classified_to_ms = 0
        self._keyword_index_version = plan.keyword_index_version

    # -- accessors -------------------------------------------------------------

    @property
    def station_id(self) -> str:
        return self._plan.station_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def status(self) -> SessionStatus:
        return self._status

    def update_keyword_index_version(self, version: int) -> None:
        """Adopt a republished index without restarting the stream.

        Reloading the index must not drop audio: the whole point of sharing one
        connection is that campaign edits are cheap.
        """
        if version != self._keyword_index_version:
            logger.info(
                "Station adopted a new keyword index",
                extra=log_fields(
                    station_id=self.station_id,
                    previous_version=self._keyword_index_version,
                    version=version,
                ),
            )
            self._keyword_index_version = version

    @property
    def started_at(self) -> datetime:
        return self._status.started_at_utc

    def is_holding(self, now: datetime, grace_seconds: float) -> bool:
        """True while this station is mid-conversation (talk without music).

        An open pure-speech segment means someone is talking right now. The
        grace window keeps the slot through the natural pauses between
        sentences, so a turn ends at a real break in speech rather than in the
        middle of one. speech_over_music does NOT hold: on music stations it is
        nearly continuous, which would pin every turn at the ceiling.
        """
        if self._segment_open and self._segment_class == "speech":
            return True
        last_speech = self._status.last_speech_at_utc
        if last_speech is None:
            return False
        return (now - last_speech).total_seconds() < grace_seconds

    def request_stop(self) -> None:
        self._stop.set()

    # -- main loop -------------------------------------------------------------

    async def run(self) -> None:
        """Connect-decode-reconnect until asked to stop."""
        attempt = 0
        settings = self._settings
        try:
            while not self._stop.is_set():
                try:
                    await self._set_status("connecting")
                    decoded = await self._stream_once()
                    if decoded > 0:
                        attempt = 0
                except asyncio.CancelledError:
                    raise
                except NetSafetyError as error:
                    # A URL that is not safe now may become safe later (DNS), but
                    # retrying fast would be a scanning loop. Back off hard.
                    attempt = max(attempt, 4)
                    await self._set_status("failed", error=f"unsafe stream URL: {error}")
                except Exception as error:  # noqa: BLE001 - a live capture must not die
                    await self._set_status("reconnecting", error=f"{type(error).__name__}: {error}")
                    logger.warning(
                        "Station stream failed",
                        extra=log_fields(
                            station_id=self.station_id,
                            error_type=type(error).__name__,
                        ),
                    )
                else:
                    await self._set_status("reconnecting", error="stream ended")

                if self._stop.is_set():
                    break

                await self._flush_segment(reason="disconnect")
                delay = reconnect_delay(
                    attempt,
                    minimum=settings.RADIO_LISTENER_RECONNECT_MIN_SECONDS,
                    maximum=settings.RADIO_LISTENER_RECONNECT_MAX_SECONDS,
                )
                attempt += 1
                self._status.reconnects += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                if not self._stop.is_set():
                    self._new_generation()
        finally:
            await self._flush_segment(reason="shutdown")
            await self._terminate_process()
            self._buffer.clear()
            await self._set_status("stopped")

    def _new_generation(self) -> None:
        """Reset all per-connection state before reconnecting.

        Audio from before and after a dropout is not contiguous. Carrying the
        buffer or the classifier's rolling state across the gap would splice two
        unrelated moments into one apparent conversation.
        """
        generation = self._buffer.reset(started_at=self._clock())
        self._policy.reset()
        self._classifier.reset()
        self._classified_to_ms = 0
        self._segment_open = False
        self._status.generation = generation

    # -- one connection --------------------------------------------------------

    async def _stream_once(self) -> int:
        settings = self._settings
        target = await asyncio.to_thread(validate_public_http_url, self._plan.stream_url)
        command = self._ffmpeg_command(target.url)

        self._process = await asyncio.create_subprocess_exec(  # noqa: S603 - validated argv
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        process = self._process
        assert process.stdout is not None  # nosec B101 - PIPE was requested

        chunk_bytes = int(bytes_per_second(settings.RADIO_SAMPLE_RATE) * READ_CHUNK_SECONDS)
        # Reads must land on whole samples or the stream desynchronises by a byte.
        chunk_bytes -= chunk_bytes % 2
        decoded = 0
        stderr_task = asyncio.create_task(self._drain_stderr(process))

        try:
            first_read = True
            while not self._stop.is_set():
                timeout = (
                    settings.RADIO_LISTENER_CONNECT_TIMEOUT_SECONDS
                    if first_read
                    else settings.RADIO_LISTENER_READ_TIMEOUT_SECONDS
                )
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(chunk_bytes), timeout=timeout
                    )
                except TimeoutError:
                    # A silent socket is indistinguishable from a hung one, and
                    # a hung one never recovers. Drop it and reconnect.
                    raise TimeoutError(
                        f"No audio for {timeout:.0f}s"
                    ) from None
                if not chunk:
                    break
                if first_read:
                    first_read = False
                    await self._set_status("streaming")
                decoded += len(chunk)
                self._status.bytes_decoded += len(chunk)
                self._status.last_audio_at_utc = self._clock()
                await self.ingest_pcm(chunk)
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
            await self._terminate_process()
        return decoded

    def _ffmpeg_command(self, url: str) -> list[str]:
        settings = self._settings
        # Microseconds. Bounds a stalled socket inside FFmpeg as well as here,
        # so a decoder cannot outlive the read timeout that supervises it.
        rw_timeout_us = int(settings.RADIO_LISTENER_READ_TIMEOUT_SECONDS * 1_000_000)
        return [
            settings.RADIO_LISTENER_FFMPEG_BINARY,
            "-nostdin",
            "-hide_banner",
            "-loglevel", "warning",
            # No `file`, no `concat`: a hostile playlist must not be able to
            # make the decoder open a local path.
            "-protocol_whitelist", "http,https,tcp,tls,crypto",
            # Internal reconnection is off on purpose; see the module docstring.
            "-reconnect", "0",
            "-rw_timeout", str(rw_timeout_us),
            "-user_agent", settings.RADIO_BROWSER_USER_AGENT,
            "-i", url,
            "-vn",
            "-ac", "1",
            "-ar", str(settings.RADIO_SAMPLE_RATE),
            "-f", "s16le",
            "pipe:1",
        ]

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Consume FFmpeg diagnostics so the pipe cannot fill and block it."""
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            self._status.last_error = text[:300]
            # DEBUG: FFmpeg is chatty about recoverable stream hiccups, and a
            # stream URL can appear in its output, which must not reach INFO.
            logger.debug(
                "ffmpeg: %s", text[:300], extra=log_fields(station_id=self.station_id)
            )

    async def _terminate_process(self) -> None:
        """SIGTERM, then SIGKILL, then reap. Never leave a zombie decoder."""
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            if process is not None:
                with contextlib.suppress(Exception):
                    await process.wait()
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    # -- audio path ------------------------------------------------------------

    async def ingest_pcm(self, pcm: bytes) -> None:
        """Feed decoded 16 kHz mono s16le audio into this session.

        Public because the FFmpeg subprocess is not the only legitimate source:
        offline evaluation replays labelled recordings through exactly this path
        so that measured quality reflects the real classification and
        segmentation logic rather than a parallel implementation of it
        (``docs/QUALITY_EVALUATION.md``).
        """
        self._buffer.append(pcm, arrived_at=self._clock())
        await self._classify_ready_windows()

    async def flush(self, *, reason: str = "manual") -> None:
        """Emit any partially accumulated segment. Used at end of stream."""
        await self._flush_segment(reason=reason)

    async def _classify_ready_windows(self) -> None:
        settings = self._settings
        window_ms = int(settings.RADIO_CLASSIFIER_WINDOW_SECONDS * 1000)
        while self._buffer.position_ms - self._classified_to_ms >= window_ms:
            start = self._classified_to_ms
            end = start + window_ms
            window = self._buffer.window(start, end)
            self._classified_to_ms = end
            if window.is_empty:
                continue
            # Classification is CPU-bound pure Python; off the event loop so one
            # station's analysis cannot stall every other station's reads.
            result = await asyncio.to_thread(self._classifier.classify, window)
            decision = self._policy.observe(result)
            await self._apply_decision(decision, start, end)

    async def _apply_decision(self, decision: Any, start_ms: int, end_ms: int) -> None:
        settings = self._settings
        chunk_ms = settings.RADIO_SPEECH_CHUNK_SECONDS * 1000
        overlap_ms = int(settings.RADIO_CHUNK_OVERLAP_SECONDS * 1000)

        if not decision.keep:
            if self._segment_open:
                await self._flush_segment(reason=decision.content_class)
            return

        if not self._segment_open:
            lead_ms = int(SPEECH_LEAD_SECONDS * 1000)
            self._segment_start_ms = max(self._buffer.oldest_offset_ms, start_ms - lead_ms)
            self._segment_open = True
            self._segment_class = decision.content_class
            self._segment_confidence = decision.confidence
            self._segment_signals = dict(decision.signals)
        else:
            # Keep the most cautious label seen inside the segment: a chunk that
            # was partly speech-over-music is speech-over-music.
            if decision.content_class == "speech_over_music":
                self._segment_class = "speech_over_music"
            self._segment_confidence = min(self._segment_confidence, decision.confidence)

        self._segment_end_ms = end_ms
        # Only talk without music holds a rotation turn. Music stations emit
        # near-continuous speech_over_music (vocals count), which pinned every
        # turn at the MAX_SLICE ceiling and made the 2-minute slice meaningless.
        if decision.content_class == "speech":
            self._status.last_speech_at_utc = self._clock()
        if self._segment_end_ms - self._segment_start_ms >= chunk_ms:
            await self._flush_segment(reason="chunk_full")
            # Overlap so a keyword straddling a chunk boundary is still fully
            # present in one of the two transcripts.
            self._segment_open = True
            self._segment_start_ms = max(
                self._buffer.oldest_offset_ms, self._segment_end_ms - overlap_ms
            )

    async def _flush_segment(self, *, reason: str) -> None:
        if not self._segment_open:
            return
        self._segment_open = False
        start_ms = self._segment_start_ms
        end_ms = self._segment_end_ms
        if end_ms <= start_ms:
            return

        try:
            window = self._buffer.window(start_ms, end_ms)
        except Exception as error:  # noqa: BLE001 - a lost window must not kill the session
            logger.warning(
                "Could not extract a segment window",
                extra=log_fields(station_id=self.station_id, error_type=type(error).__name__),
            )
            return
        if window.is_empty:
            return

        self._sequence += 1
        trace_id = new_id()
        event = SegmentEvent(
            station_id=self.station_id,
            station_session_id=self._session_id,
            sequence_number=self._sequence,
            pcm=window.pcm,
            sample_rate=window.sample_rate,
            started_at=window.started_at,
            ended_at=window.ended_at,
            duration_ms=window.duration_ms,
            content_class=self._segment_class,
            content_class_confidence=self._segment_confidence,
            classifier_signals=dict(self._segment_signals),
            keyword_index_version=self._keyword_index_version,
            trace_id=trace_id,
            generation=window.generation,
        )
        self._status.segments_emitted += 1
        with trace_context(trace_id):
            logger.info(
                "Segment retained",
                extra=log_fields(
                    station_id=self.station_id,
                    trace_id=trace_id,
                    sequence_number=event.sequence_number,
                    duration_ms=event.duration_ms,
                    content_class=event.content_class,
                    reason=reason,
                ),
            )
            await self._emit(event)

    # -- status ----------------------------------------------------------------

    async def _set_status(self, status: str, *, error: str | None = None) -> None:
        self._status.status = status
        if error:
            self._status.last_error = error[:300]
        if self._on_status is not None:
            with contextlib.suppress(Exception):
                # Status reporting is best-effort: a database hiccup must never
                # interrupt a live capture.
                await self._on_status(self._status)


class StreamSupervisor:
    """Owns every station session in this listener process."""

    def __init__(
        self,
        settings: Settings,
        *,
        classifier_factory: Callable[[], AudioClassifier],
        emit: SegmentSink,
        on_status: StatusSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._classifier_factory = classifier_factory
        self._emit = emit
        self._on_status = on_status
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions: dict[str, StationSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopping = False
        #: station_id -> when it last started a turn. Absent means never served,
        #: which sorts first so a new station is not stuck behind the incumbents.
        self._last_served: dict[str, datetime] = {}
        #: station_id -> most recent confirmed keyword hit, supplied per
        #: reconcile by the listener from the shared database.
        self._keyword_hits: dict[str, datetime] = {}
        # Rate limiting for the queue log: (signature, last logged at).
        self._queue_log_signature: tuple[int, int] | None = None
        self._queue_log_at: datetime | None = None

    @property
    def active_station_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sessions))

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def status_snapshot(self) -> list[dict[str, Any]]:
        return [session.status.as_dict() for session in self._sessions.values()]

    async def reconcile(
        self,
        plans: list[StationPlan],
        keyword_hits: dict[str, datetime] | None = None,
    ) -> dict[str, int]:
        """Converge running sessions on ``plans``.

        Admission is bounded by ``RADIO_LISTENER_MAX_SESSIONS``. Overflow is
        reported, never silently dropped: a station that is not being listened
        to must be visible as such (the planner parks it in
        ``pending_capacity``).
        """
        if self._stopping:
            return {"started": 0, "stopped": 0, "running": len(self._sessions), "rejected": 0}

        if keyword_hits is not None:
            self._keyword_hits = dict(keyword_hits)
        wanted = {plan.station_id: plan for plan in plans}
        stopped = 0
        for station_id in list(self._sessions):
            if station_id not in wanted:
                await self._stop_session(station_id)
                stopped += 1

        limit = self._settings.RADIO_LISTENER_MAX_SESSIONS
        now = self._clock()

        # Keep the fairness ledger to the stations we still care about, so it
        # cannot grow without bound as campaigns come and go.
        self._last_served = {
            station_id: served
            for station_id, served in self._last_served.items()
            if station_id in wanted
        }

        for station_id, plan in wanted.items():
            existing = self._sessions.get(station_id)
            if existing is not None:
                existing.update_keyword_index_version(plan.keyword_index_version)

        waiting = [station_id for station_id in wanted if station_id not in self._sessions]

        # Rotation: a slot is a turn, not a permanent home. Only evict when
        # somebody is actually waiting -- with spare capacity every station
        # simply keeps listening.
        rotated = 0
        if waiting:
            rotated = await self._rotate_expired_turns(now, len(waiting))

        # Fairest first: longest since its last turn, never-served before all.
        started = 0
        free = max(limit - len(self._sessions), 0)
        order = sorted(waiting, key=lambda sid: (self._last_served.get(sid, _EPOCH), sid))
        for station_id in order[:free]:
            self._start_session(wanted[station_id])
            self._last_served[station_id] = now
            started += 1

        rejected = max(len(waiting) - started, 0)
        if rejected:
            # Expected while rotating; the queue drains a turn at a time. Log
            # on change or once a minute -- every 5s tick buried the journal.
            signature = (rejected, len(self._sessions))
            stale = (
                self._queue_log_at is None
                or (now - self._queue_log_at).total_seconds() >= 60
            )
            if signature != self._queue_log_signature or stale or rotated:
                self._queue_log_signature = signature
                self._queue_log_at = now
                logger.info(
                    "Stations are queued for a listening turn",
                    extra=log_fields(
                        queued=rejected,
                        listener_max_sessions=limit,
                        running=len(self._sessions),
                        rotated_out=rotated,
                        slice_seconds=self._settings.RADIO_LISTENER_SLICE_SECONDS,
                    ),
                )
        self._reap_finished()
        return {
            "started": started,
            "stopped": stopped,
            "running": len(self._sessions),
            "rejected": rejected,
            "rotated": rotated,
        }

    async def _rotate_expired_turns(self, now: datetime, waiting_count: int) -> int:
        """Free up to ``waiting_count`` slots from sessions whose turn is over.

        A turn ends once it has run for the slice AND the station is not
        mid-conversation. The max-slice ceiling overrides the dwell so a
        permanently talking station can never hold a slot forever.
        """
        slice_seconds = self._settings.RADIO_LISTENER_SLICE_SECONDS
        max_slice = self._settings.RADIO_LISTENER_MAX_SLICE_SECONDS
        grace = self._settings.RADIO_LISTENER_DWELL_GRACE_SECONDS

        # Oldest turn first, so the station that has held a slot longest goes.
        candidates = sorted(
            self._sessions.items(), key=lambda item: item[1].started_at
        )
        rotated = 0
        for station_id, session in candidates:
            if rotated >= waiting_count:
                break
            elapsed = (now - session.started_at).total_seconds()
            if elapsed < slice_seconds:
                continue
            if elapsed < max_slice and self._keyword_dwell_holds(station_id, session, now):
                # A keyword was just spoken here: this is the one station we
                # know is talking about the thing we monitor, so it keeps the
                # slot for the keyword dwell window.
                continue
            if elapsed < max_slice and session.is_holding(now, grace):
                # Still talking: finishing the sentence is worth more than
                # switching exactly on the slice boundary.
                continue
            await self._stop_session(station_id)
            self._last_served[station_id] = now
            rotated += 1
            logger.info(
                "Listening turn handed over",
                extra=log_fields(
                    station_id=station_id,
                    held_seconds=round(elapsed, 1),
                    slice_seconds=slice_seconds,
                ),
            )
        return rotated

    def _keyword_dwell_holds(
        self, station_id: str, session: StationSession, now: datetime
    ) -> bool:
        """True while a keyword hit from THIS turn is inside its dwell window."""
        dwell = self._settings.RADIO_LISTENER_KEYWORD_DWELL_SECONDS
        if dwell <= 0:
            return False
        hit = self._keyword_hits.get(station_id)
        if hit is None or hit < session.started_at:
            return False
        return (now - hit).total_seconds() < dwell

    def _start_session(self, plan: StationPlan) -> None:
        session = StationSession(
            plan,
            self._settings,
            classifier=self._classifier_factory(),
            emit=self._emit,
            on_status=self._on_status,
            clock=self._clock,
        )
        self._sessions[plan.station_id] = session
        task = asyncio.create_task(session.run(), name=f"station:{plan.station_id}")
        self._tasks[plan.station_id] = task
        logger.info(
            "Station session started",
            extra=log_fields(station_id=plan.station_id, station_session_id=session.session_id),
        )

    async def _stop_session(self, station_id: str) -> None:
        session = self._sessions.pop(station_id, None)
        task = self._tasks.pop(station_id, None)
        if session is not None:
            session.request_stop()
        if task is not None:
            try:
                await asyncio.wait_for(
                    task, timeout=self._settings.RADIO_LISTENER_SHUTDOWN_GRACE_SECONDS
                )
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        logger.info("Station session stopped", extra=log_fields(station_id=station_id))

    def _reap_finished(self) -> None:
        """Drop sessions whose task exited on its own, so they can restart."""
        for station_id, task in list(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(station_id, None)
            self._sessions.pop(station_id, None)
            if not task.cancelled() and task.exception() is not None:
                logger.error(
                    "Station session task ended unexpectedly",
                    extra=log_fields(
                        station_id=station_id,
                        error_type=type(task.exception()).__name__,
                    ),
                )

    async def shutdown(self) -> None:
        """Stop every session, bounded by the configured grace period.

        Called from the SIGTERM handler. Sessions are asked to stop in parallel
        so shutdown is bounded by the slowest one, not by their sum.
        """
        self._stopping = True
        for session in self._sessions.values():
            session.request_stop()
        tasks = list(self._tasks.values())
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=self._settings.RADIO_LISTENER_SHUTDOWN_GRACE_SECONDS
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._sessions.clear()
        self._tasks.clear()


__all__ = [
    "READ_CHUNK_SECONDS",
    "SPEECH_LEAD_SECONDS",
    "TERMINATE_GRACE_SECONDS",
    "SegmentEvent",
    "SegmentSink",
    "SessionStatus",
    "StationPlan",
    "StationSession",
    "StatusSink",
    "StreamSupervisor",
    "reconnect_delay",
]
