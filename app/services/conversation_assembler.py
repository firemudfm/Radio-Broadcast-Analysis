"""Per-station conversation assembly (ADR-010 §6).

One ordered state machine per station turns a stream of transcribed segments
into *physical conversations*: the complete stretch of speech surrounding a
keyword, including the context before it was said.

    idle -> candidate -> open -> closing -> closed
                   \\-> failed

A conversation opens when a segment produces a candidate match, and it reaches
backwards: ``RADIO_PRE_KEYWORD_SECONDS`` of already-transcribed speech is pulled
in, because the sentence that introduces a brand almost never contains it. It
then keeps collecting until one of five bounded end conditions fires.

The output is deliberately **one** of everything -- one conversation, one
transcript, one analysis job, one evidence clip -- and *many* mapping rows. A
discussion mentioning two brands tracked by five campaigns is transcribed once,
analysed once, and attributed five times. That asymmetry is the reason this
class exists.

Ordering assumptions
--------------------
Segments arrive in per-station order because the transcription queue uses
``MessageGroupId = station_id`` (ADR-003). The assembler does not *trust* that:
duplicates are ignored, out-of-order arrivals are rejected rather than
reordered, and sequence gaps are recorded. Gaps are normal -- discarded music
consumes no sequence number in the retained stream -- so a gap alone never ends
a conversation. Only elapsed time does.

State is held in memory and rebuilt from ``conversation_sessions`` on restart,
so the class stays pure and testable while durability lives in the worker.
"""
from __future__ import annotations

import logging
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..observability import log_fields
from ..pipeline.enums import ConversationCloseReason, ConversationState
from ..pipeline.ids import new_id
from .keyword_matcher import KeywordMatch

logger = logging.getLogger(__name__)

#: Namespace for deterministic conversation ids.
_CONVERSATION_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def conversation_id_for(station_session_id: str, first_sequence_number: int) -> str:
    """Derive a stable conversation id from where the conversation started.

    Deterministic rather than random, and that matters for correctness rather
    than tidiness. A redelivered segment processed by a *restarted* worker has
    no in-memory assembler state to recognise it, so a random id would open a
    second conversation and produce a duplicate mention. Deriving the id from
    ``(station_session_id, first sequence number)`` makes the replay collide
    with the original, and every downstream write is keyed on
    ``conversation_id`` -- so the duplicate collapses into a no-op instead.
    """
    return str(
        uuid.uuid5(_CONVERSATION_NAMESPACE, f"{station_session_id}:{first_sequence_number}")
    )

#: Cap on retained pre-roll segments per station, independent of the time
#: window. A station emitting many very short segments must not grow the
#: assembler's memory without bound.
MAX_PREROLL_SEGMENTS = 40

#: Cap on segments in one conversation. Belt-and-braces alongside the duration
#: limit, which a stream with broken timestamps could otherwise evade.
MAX_CONVERSATION_SEGMENTS = 200


@dataclass(frozen=True)
class TranscribedSegment:
    """One transcribed segment, ready to be folded into a conversation."""

    segment_id: str
    station_id: str
    station_session_id: str
    sequence_number: int
    transcript_id: str
    text: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    content_class: str = "speech"
    language: str | None = None
    language_probability: float | None = None
    matches: tuple[KeywordMatch, ...] = ()
    trace_id: str = ""

    @property
    def has_candidate(self) -> bool:
        return bool(self.matches)


@dataclass(frozen=True)
class ClosedConversation:
    """A finished conversation, ready for analysis and attribution."""

    conversation_id: str
    station_id: str
    station_session_id: str
    close_reason: ConversationCloseReason
    first_sequence_number: int
    last_sequence_number: int
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    transcript_text: str
    detected_language: str | None
    segments: tuple[TranscribedSegment, ...]
    matches: tuple[KeywordMatch, ...]
    missing_sequences: tuple[int, ...]
    trace_id: str
    state: ConversationState = "closed"

    @property
    def campaign_ids(self) -> tuple[str, ...]:
        """Every campaign this one conversation belongs to."""
        return tuple(sorted({cid for match in self.matches for cid in match.campaign_ids}))

    @property
    def keyword_ids(self) -> tuple[str, ...]:
        return tuple(sorted({match.keyword_id for match in self.matches}))

    @property
    def requires_confirmation(self) -> bool:
        """True when every match is a candidate that pass B must still confirm."""
        return bool(self.matches) and all(match.requires_confirmation for match in self.matches)

    @property
    def transcript_ids(self) -> tuple[str, ...]:
        return tuple(segment.transcript_id for segment in self.segments)

    @property
    def evidence_start(self) -> datetime:
        return self.started_at

    @property
    def evidence_end(self) -> datetime:
        return self.ended_at


@dataclass
class _StationState:
    """Mutable per-station assembly state."""

    station_id: str
    station_session_id: str | None = None
    state: ConversationState = "idle"
    conversation_id: str | None = None
    trace_id: str = ""
    segments: list[TranscribedSegment] = field(default_factory=list)
    matches: list[KeywordMatch] = field(default_factory=list)
    preroll: deque[TranscribedSegment] = field(
        default_factory=lambda: deque(maxlen=MAX_PREROLL_SEGMENTS)
    )
    last_sequence: int | None = None
    seen_sequences: set[int] = field(default_factory=set)
    missing_sequences: list[int] = field(default_factory=list)
    last_segment_end: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.state in {"candidate", "open"}


class ConversationAssembler:
    """Assembles conversations for every station this worker sees."""

    def __init__(self, settings: Settings, *, clock=None) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stations: dict[str, _StationState] = {}

    # -- introspection ---------------------------------------------------------

    def state_of(self, station_id: str) -> ConversationState:
        station = self._stations.get(station_id)
        return station.state if station else "idle"

    def open_conversation_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                station.conversation_id
                for station in self._stations.values()
                if station.is_open and station.conversation_id
            )
        )

    # -- main entry point ------------------------------------------------------

    def observe(self, segment: TranscribedSegment) -> list[ClosedConversation]:
        """Fold one transcribed segment in; return any conversations it closed.

        A list, not an optional: a segment can both close the previous
        conversation (its timestamp shows a long gap) and open a new one.
        """
        station = self._stations.setdefault(
            segment.station_id, _StationState(station_id=segment.station_id)
        )
        closed: list[ClosedConversation] = []

        if not self._accept(station, segment):
            return closed

        # A new station session means the stream reconnected. Audio either side
        # of a dropout is not one conversation.
        if (
            station.station_session_id is not None
            and station.station_session_id != segment.station_session_id
        ):
            finished = self._close(station, reason="disconnect")
            if finished:
                closed.append(finished)
            self._reset(station)
        station.station_session_id = segment.station_session_id

        if station.is_open:
            gap = self._gap_seconds(station, segment)
            if gap is not None and gap >= self._settings.RADIO_SILENCE_END_SECONDS:
                # Retained segments are contiguous speech; a long wall-clock gap
                # between them means the audio in between was discarded as
                # silence or music, which is where a conversation ends.
                reason: ConversationCloseReason = (
                    "music" if segment.content_class in {"music", "singing"} else "silence"
                )
                finished = self._close(station, reason=reason)
                if finished:
                    closed.append(finished)

        self._record_sequence(station, segment)

        if station.is_open:
            station.segments.append(segment)
            station.matches.extend(segment.matches)
            if segment.matches:
                station.state = "open"
        elif segment.has_candidate:
            self._open(station, segment)
        else:
            station.preroll.append(segment)
            self._trim_preroll(station)

        station.last_segment_end = segment.ended_at

        if station.is_open:
            finished = self._close_if_exhausted(station)
            if finished:
                closed.append(finished)
        return closed

    # -- lifecycle -------------------------------------------------------------

    def _open(self, station: _StationState, segment: TranscribedSegment) -> None:
        """Start a conversation, reaching back for pre-keyword context."""
        preroll = self._preroll_for(station, segment)
        station.state = "open" if segment.matches else "candidate"
        first = preroll[0] if preroll else segment
        station.conversation_id = conversation_id_for(
            segment.station_session_id, first.sequence_number
        )
        station.trace_id = segment.trace_id or new_id()
        station.segments = [*preroll, segment]
        station.matches = list(segment.matches)
        station.preroll.clear()
        logger.info(
            "Conversation opened",
            extra=log_fields(
                station_id=station.station_id,
                conversation_id=station.conversation_id,
                trace_id=station.trace_id,
                preroll_segments=len(preroll),
                keyword_count=len(segment.matches),
            ),
        )

    def _preroll_for(
        self, station: _StationState, segment: TranscribedSegment
    ) -> list[TranscribedSegment]:
        """Recent speech within the pre-roll window, in order.

        The sentence that introduces a brand usually does not contain it, so a
        mention without its lead-in reads as a fragment.
        """
        window = timedelta(seconds=self._settings.RADIO_PRE_KEYWORD_SECONDS)
        cutoff = segment.started_at - window
        selected = [item for item in station.preroll if item.ended_at >= cutoff]
        return selected

    def _trim_preroll(self, station: _StationState) -> None:
        if not station.preroll:
            return
        window = timedelta(seconds=self._settings.RADIO_PRE_KEYWORD_SECONDS)
        cutoff = station.preroll[-1].ended_at - window
        while station.preroll and station.preroll[0].ended_at < cutoff:
            station.preroll.popleft()

    def _close_if_exhausted(self, station: _StationState) -> ClosedConversation | None:
        if not station.segments:
            return None
        duration = (
            station.segments[-1].ended_at - station.segments[0].started_at
        ).total_seconds()
        if duration >= self._settings.RADIO_MAX_CONVERSATION_SECONDS:
            return self._close(station, reason="max_duration")
        if len(station.segments) >= MAX_CONVERSATION_SEGMENTS:
            return self._close(station, reason="max_duration")
        return None

    def close(
        self, station_id: str, *, reason: ConversationCloseReason = "shutdown"
    ) -> ClosedConversation | None:
        """Close a station's open conversation, e.g. on shutdown or disconnect."""
        station = self._stations.get(station_id)
        if station is None:
            return None
        return self._close(station, reason=reason)

    def close_all(
        self, *, reason: ConversationCloseReason = "shutdown"
    ) -> list[ClosedConversation]:
        closed = []
        for station in self._stations.values():
            finished = self._close(station, reason=reason)
            if finished:
                closed.append(finished)
        return closed

    def _close(
        self, station: _StationState, *, reason: ConversationCloseReason
    ) -> ClosedConversation | None:
        """Emit the conversation, or nothing if there is no keyword evidence.

        A conversation with no matches is not a mention. It is dropped here and
        its audio becomes eligible for cleanup -- that is what keeps the spool
        bounded on a station nobody's keywords appear on.
        """
        if not station.is_open or not station.segments:
            self._reset(station)
            return None

        station.state = "closing"
        segments = tuple(station.segments)
        matches = tuple(_deduplicate_matches(station.matches))
        conversation_id = station.conversation_id or new_id()

        if not matches:
            logger.debug(
                "Conversation closed without keyword evidence; not a mention",
                extra=log_fields(
                    station_id=station.station_id, conversation_id=conversation_id
                ),
            )
            self._reset(station)
            return None

        started_at = segments[0].started_at
        ended_at = segments[-1].ended_at
        closed = ClosedConversation(
            conversation_id=conversation_id,
            station_id=station.station_id,
            station_session_id=station.station_session_id or "",
            close_reason=reason,
            first_sequence_number=segments[0].sequence_number,
            last_sequence_number=segments[-1].sequence_number,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=max(0, int((ended_at - started_at).total_seconds() * 1000)),
            transcript_text=" ".join(
                segment.text.strip() for segment in segments if segment.text.strip()
            ),
            detected_language=_dominant_language(segments),
            segments=segments,
            matches=matches,
            missing_sequences=tuple(sorted(station.missing_sequences)),
            trace_id=station.trace_id or new_id(),
        )
        logger.info(
            "Conversation closed",
            extra=log_fields(
                station_id=station.station_id,
                conversation_id=conversation_id,
                trace_id=closed.trace_id,
                close_reason=reason,
                duration_ms=closed.duration_ms,
                segment_count=len(segments),
                campaign_count=len(closed.campaign_ids),
                keyword_count=len(closed.keyword_ids),
            ),
        )
        self._reset(station)
        return closed

    def _reset(self, station: _StationState) -> None:
        station.state = "idle"
        station.conversation_id = None
        station.trace_id = ""
        station.segments = []
        station.matches = []
        station.missing_sequences = []

    # -- ordering guards -------------------------------------------------------

    def _accept(self, station: _StationState, segment: TranscribedSegment) -> bool:
        """Reject duplicates and late arrivals rather than reordering them."""
        if segment.station_session_id != station.station_session_id:
            return True  # A new session restarts the sequence space.
        if segment.sequence_number in station.seen_sequences:
            logger.debug(
                "Ignoring a duplicate segment",
                extra=log_fields(
                    station_id=station.station_id,
                    segment_id=segment.segment_id,
                    sequence_number=segment.sequence_number,
                ),
            )
            return False
        if station.last_sequence is not None and segment.sequence_number < station.last_sequence:
            # Reordering would splice audio out of chronological order and
            # produce a transcript that never happened.
            logger.warning(
                "Rejecting an out-of-order segment",
                extra=log_fields(
                    station_id=station.station_id,
                    segment_id=segment.segment_id,
                    sequence_number=segment.sequence_number,
                    last_sequence_number=station.last_sequence,
                ),
            )
            return False
        return True

    def _record_sequence(self, station: _StationState, segment: TranscribedSegment) -> None:
        if station.last_sequence is not None and segment.sequence_number > station.last_sequence + 1:
            # Expected whenever music was discarded between two speech runs.
            # Recorded for the audit trail, never treated as an error.
            station.missing_sequences.extend(
                range(station.last_sequence + 1, segment.sequence_number)
            )
        station.last_sequence = segment.sequence_number
        station.seen_sequences.add(segment.sequence_number)
        if len(station.seen_sequences) > 4 * MAX_CONVERSATION_SEGMENTS:
            # Keep only recent sequence numbers: duplicate detection needs a
            # window, not an unbounded set on a stream that runs for weeks.
            cutoff = segment.sequence_number - 2 * MAX_CONVERSATION_SEGMENTS
            station.seen_sequences = {
                value for value in station.seen_sequences if value > cutoff
            }

    @staticmethod
    def _gap_seconds(station: _StationState, segment: TranscribedSegment) -> float | None:
        if station.last_segment_end is None:
            return None
        return max(0.0, (segment.started_at - station.last_segment_end).total_seconds())


# --- helpers ------------------------------------------------------------------


def _deduplicate_matches(matches: Iterable[KeywordMatch]) -> list[KeywordMatch]:
    """One row per keyword per conversation, keeping the strongest hit.

    A brand said five times is one mention of that brand, not five.
    """
    best: dict[str, KeywordMatch] = {}
    for match in matches:
        current = best.get(match.keyword_id)
        if current is None or match.confidence > current.confidence:
            best[match.keyword_id] = match
    return sorted(best.values(), key=lambda match: (match.start_char, match.keyword_id))


def _dominant_language(segments: Iterable[TranscribedSegment]) -> str | None:
    """The language covering the most transcript text.

    Length-weighted rather than a simple majority of segments: in a
    code-switched broadcast the language of the substance matters more than the
    language of the most fragments.
    """
    weights: dict[str, int] = {}
    for segment in segments:
        if not segment.language:
            continue
        weights[segment.language] = weights.get(segment.language, 0) + len(segment.text)
    if not weights:
        return None
    return max(weights, key=lambda code: weights[code])


__all__ = [
    "MAX_CONVERSATION_SEGMENTS",
    "MAX_PREROLL_SEGMENTS",
    "ClosedConversation",
    "ConversationAssembler",
    "TranscribedSegment",
    "conversation_id_for",
]
