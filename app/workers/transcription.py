"""Transcription worker: segment -> transcript -> match -> conversation.

Consumes the transcription queue and, for each segment:

1. verifies the recorded SHA-256 before the bytes reach a decoder;
2. runs pass-A ASR;
3. matches the transcript against the station's **combined** keyword index --
   once, for every campaign;
4. folds the result into that station's conversation assembler;
5. when a conversation closes with keyword evidence, commits it and enqueues
   exactly one analysis job.

The queue's ``MessageGroupId = station_id`` is what makes step 4 sound: SQS FIFO
delivers one message per group at a time, so a station's segments arrive in
order and one assembler instance per station is enough.

Steps 4-5 write the conversation and its outbox event in one transaction, then
the message is deleted. A crash between them redelivers the segment, and the
inbox turns the redelivery into a no-op.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from ..observability import log_fields, trace_context
from ..pipeline import outbox
from ..pipeline.contracts import (
    AnalysisJobV1,
    MatchedKeywordRef,
    TranscriptReference,
    parse_transcription_job,
)
from ..pipeline.errors import SegmentMissingError
from ..pipeline.factory import (
    ANALYSIS_QUEUE,
    TRANSCRIPTION_QUEUE,
    build_queue,
    build_segment_store,
)
from ..pipeline.idempotency import MessageProcessor, ProcessingOutcome
from ..pipeline.ids import new_id
from ..pipeline.queue import ReceivedMessage
from ..services.content_classifier import build_content_classifier
from ..services.conversation_assembler import (
    ClosedConversation,
    ConversationAssembler,
    TranscribedSegment,
)
from ..services.keyword_matcher import KeywordMatcher, Timeline
from ..services.subscription_planner import SubscriptionPlanner
from ..services.transcription import TranscriptionResult, TranscriptionService
from . import BaseWorker, bootstrap

logger = logging.getLogger(__name__)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class TranscriptionWorker(BaseWorker):
    role = "transcription"

    def __init__(
        self,
        settings,
        database,
        *,
        queue=None,
        segment_store=None,
        transcription_service=None,
        **kwargs,
    ) -> None:
        super().__init__(settings, database, **kwargs)
        self.queue = queue or build_queue(settings, TRANSCRIPTION_QUEUE)
        self.store = segment_store or build_segment_store(settings)
        self.transcriber = transcription_service or TranscriptionService(settings)
        self.assembler = ConversationAssembler(settings)
        self.content_classifier = build_content_classifier(settings)
        self.planner = SubscriptionPlanner(settings, database)
        self.processor = MessageProcessor(
            database=database,
            queue=self.queue,
            queue_name=TRANSCRIPTION_QUEUE,
            component="transcription_worker",
            visibility_seconds=settings.RADIO_SQS_VISIBILITY_SECONDS,
            heartbeat_seconds=settings.RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS,
            max_processing_seconds=settings.RADIO_SQS_MAX_PROCESSING_SECONDS,
            max_messages=settings.RADIO_SQS_MAX_MESSAGES_PER_RECEIVE,
            wait_seconds=settings.RADIO_SQS_WAIT_TIME_SECONDS,
        )
        self._matchers: dict[tuple[str, int], KeywordMatcher] = {}
        self.stats = {"segments": 0, "matched": 0, "conversations": 0, "stale_skipped": 0}

    def tick(self) -> bool:
        result = self.processor.poll_once(self.handle)
        return result["received"] == 0

    def shutdown(self) -> None:
        """Flush open conversations so in-progress evidence is not lost."""
        for closed in self.assembler.close_all(reason="shutdown"):
            try:
                self._commit_conversation(closed)
            except Exception:  # noqa: BLE001 - shutdown must always complete
                logger.exception(
                    "Could not commit a conversation during shutdown",
                    extra=log_fields(conversation_id=closed.conversation_id),
                )

    # -- message handling ------------------------------------------------------

    def handle(self, message: ReceivedMessage) -> ProcessingOutcome:
        job = parse_transcription_job(message.body)
        with trace_context(job.trace_id):
            return self._handle_job(job, message)

    def _handle_job(self, job, message: ReceivedMessage) -> ProcessingOutcome:
        if self._job_is_stale(job):
            return self._skip_stale_job(job, message)
        # read() verifies the digest recorded at write time before returning
        # bytes, so corruption or tampering fails closed here.
        audio = self.store.read(job.storage)
        self._mark_job(job.segment_id, status="running")

        transcript = self.transcriber.transcribe(audio, language_hints=job.language_hints)
        self.stats["segments"] += 1

        matcher = self._matcher_for(job.station_id, job.keyword_index_version)
        report = (
            matcher.match(transcript.text, timeline=Timeline.from_segments(transcript.segments))
            if matcher and transcript.text
            else None
        )
        matches = report.matches if report else ()
        if matches:
            self.stats["matched"] += 1

        transcript_id = self._store_transcript(job, transcript, message)
        segment = TranscribedSegment(
            segment_id=job.segment_id,
            station_id=job.station_id,
            station_session_id=job.station_session_id,
            sequence_number=job.sequence_number,
            transcript_id=transcript_id,
            text=transcript.text,
            started_at=job.started_at,
            ended_at=job.started_at + _duration(job.duration_ms),
            duration_ms=job.duration_ms,
            content_class=job.content_class,
            language=transcript.language,
            language_probability=transcript.language_probability,
            matches=matches,
            trace_id=job.trace_id,
        )

        for closed in self.assembler.observe(segment):
            self._commit_conversation(closed)

        self._mark_job(job.segment_id, status="succeeded")
        self._mark_disposition(job.segment_id, retained=bool(matches))
        return ProcessingOutcome(handled=True, result_reference=transcript_id)

    # -- backlog freshness -----------------------------------------------------

    def _job_is_stale(self, job) -> bool:
        age = datetime.now(UTC) - job.created_at
        return age > timedelta(hours=self.settings.RADIO_TRANSCRIPTION_MAX_AGE_HOURS)

    def _skip_stale_job(self, job, message: ReceivedMessage) -> ProcessingOutcome:
        """Acknowledge an expired job without paying for a decode.

        Monitoring is about NOW. When a backlog builds, spending 5-10 seconds
        of CPU on every day-old segment starves the fresh ones behind it and
        the queue can never drain. The segment's audio is released to cleanup;
        the inbox row makes a redelivery of the same message a no-op.
        """
        stamp = _iso(datetime.now(UTC))

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE transcription_jobs SET status='abandoned',"
                " attempts=attempts+1, worker_id=?, updated_at_utc=?"
                " WHERE segment_id=?",
                (self.worker_id, stamp, job.segment_id),
            )
            connection.execute(
                "UPDATE audio_segments SET disposition='disposable',"
                " updated_at_utc=? WHERE segment_id=?"
                " AND disposition NOT IN ('retained', 'deleted')",
                (stamp, job.segment_id),
            )
            self.processor.inbox.record_processed(
                connection, message, result_reference="stale-skip", trace_id=job.trace_id
            )

        self.database.write(write)
        self.stats["stale_skipped"] += 1
        logger.info(
            "Skipped a stale transcription job",
            extra=log_fields(
                segment_id=job.segment_id,
                station_id=job.station_id,
                age_hours=round(
                    (datetime.now(UTC) - job.created_at).total_seconds() / 3600, 1
                ),
                trace_id=job.trace_id,
            ),
        )
        return ProcessingOutcome(handled=True, result_reference="stale-skip")

    # -- persistence -----------------------------------------------------------

    def _store_transcript(
        self, job, transcript: TranscriptionResult, message: ReceivedMessage
    ) -> str:
        """Persist the transcript and acknowledge the message in ONE transaction.

        The inbox row is what makes redelivery a no-op. SQS FIFO deduplication
        only lasts five minutes, so a redelivery after a longer outage really
        does arrive; without this row the segment would be transcribed again and
        -- in a restarted worker with no assembler state -- could fork a second
        conversation. Committing it with the business result is the guarantee.
        """
        import json

        transcript_id = new_id()
        stamp = _iso(datetime.now(UTC))
        payload = transcript.as_payload()

        def write(connection: sqlite3.Connection) -> str:
            connection.execute(
                """
                INSERT INTO transcripts(
                  transcript_id, segment_id, station_id, asr_pass, text,
                  detected_language, language_probability, segments_json, words_json,
                  model_name, model_revision, compute_type, beam_size, duration_ms,
                  created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(segment_id, asr_pass) DO NOTHING
                """,
                (
                    transcript_id,
                    job.segment_id,
                    job.station_id,
                    transcript.asr_pass,
                    transcript.text,
                    transcript.language,
                    transcript.language_probability,
                    json.dumps(payload["segments"], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    transcript.model_name,
                    transcript.model_revision,
                    transcript.compute_type,
                    transcript.beam_size,
                    transcript.duration_ms,
                    stamp,
                ),
            )
            self.processor.inbox.record_processed(
                connection, message, result_reference=transcript_id, trace_id=job.trace_id
            )
            row = connection.execute(
                "SELECT transcript_id FROM transcripts WHERE segment_id=? AND asr_pass=?",
                (job.segment_id, transcript.asr_pass),
            ).fetchone()
            return str(row["transcript_id"]) if row else transcript_id

        return self.database.write(write)

    def _commit_conversation(self, closed: ClosedConversation) -> None:
        """Persist the conversation and enqueue exactly one analysis job."""
        decision = self.content_classifier.classify(
            closed.transcript_text,
            audio_class=closed.segments[-1].content_class if closed.segments else "speech",
            duration_ms=closed.duration_ms,
            language=closed.detected_language,
        )
        analysis_job_id = new_id()
        job = AnalysisJobV1(
            analysis_job_id=analysis_job_id,
            mention_id=new_id(),
            conversation_id=closed.conversation_id,
            station_id=closed.station_id,
            language=closed.detected_language,
            transcript_reference=TranscriptReference(
                transcript_id=closed.segments[-1].transcript_id
            ),
            # Carry the real evidence, not a summary. The analysis worker never
            # sees the audio, the per-segment transcript or the station's
            # keyword index, so anything dropped here is fabricated downstream
            # and lands in the permanent mention_keywords audit trail.
            #
            # campaign_ids is per-match ownership and is intentionally narrower
            # than the job-level campaign_ids below, which covers the whole
            # conversation.
            matched_keywords=[
                MatchedKeywordRef(
                    keyword_id=item.keyword_id,
                    campaign_ids=list(item.campaign_ids),
                    canonical_value=item.canonical_value[:200],
                    matched_text=item.matched_text[:300],
                    match_level=item.match_level,
                    start_char=item.start_char,
                    end_char=item.end_char,
                    # The wire contract types these as non-optional ints and the
                    # mention_keywords columns are NOT NULL, so an untimed match
                    # coerces to 0 here. That coercion already existed; it is not
                    # new information loss.
                    start_ms=item.start_ms or 0,
                    end_ms=item.end_ms or 0,
                    confidence=item.confidence,
                )
                for item in closed.matches
            ],
            campaign_ids=list(closed.campaign_ids),
            trace_id=closed.trace_id,
            created_at=datetime.now(UTC),
        )
        body = job.to_body()
        stamp = _iso(datetime.now(UTC))

        def write(connection: sqlite3.Connection) -> None:
            import json

            connection.execute(
                """
                INSERT INTO conversation_sessions(
                  conversation_id, station_id, station_session_id, state, close_reason,
                  first_sequence_number, last_sequence_number, started_at_utc, ended_at_utc,
                  duration_ms, transcript_text, detected_language, content_type,
                  content_type_confidence, missing_sequences_json, trace_id,
                  created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO NOTHING
                """,
                (
                    closed.conversation_id,
                    closed.station_id,
                    closed.station_session_id,
                    closed.close_reason,
                    closed.first_sequence_number,
                    closed.last_sequence_number,
                    _iso(closed.started_at),
                    _iso(closed.ended_at),
                    closed.duration_ms,
                    closed.transcript_text,
                    closed.detected_language,
                    decision.content_type,
                    decision.confidence,
                    json.dumps(list(closed.missing_sequences)),
                    closed.trace_id,
                    stamp,
                    stamp,
                ),
            )
            # Stamp the member transcripts with their conversation. The INSERT
            # in _store_transcript runs before any conversation exists, so the
            # column starts NULL; without this stamp the detail view's
            # "transcripts WHERE conversation_id=?" matches nothing and every
            # pipeline mention renders an empty full transcript.
            segment_ids = [segment.segment_id for segment in closed.segments]
            if segment_ids:
                placeholders = ",".join("?" for _ in segment_ids)
                connection.execute(
                    "UPDATE transcripts SET conversation_id=?"  # nosec B608 (only '?' is interpolated)
                    f" WHERE segment_id IN ({placeholders})",
                    (closed.conversation_id, *segment_ids),
                )
            # Deduplicated on conversation_id by the outbox's UNIQUE constraint,
            # so a redelivered segment cannot produce a second analysis job.
            outbox.enqueue(
                connection,
                queue_name=ANALYSIS_QUEUE,
                message_group_id=job.message_group_id(),
                message_deduplication_id=job.deduplication_id(),
                payload=body,
                trace_id=closed.trace_id,
            )

        self.database.write(write)
        self.stats["conversations"] += 1
        logger.info(
            "Conversation queued for analysis",
            extra=log_fields(
                conversation_id=closed.conversation_id,
                station_id=closed.station_id,
                trace_id=closed.trace_id,
                content_type=decision.content_type,
                campaign_count=len(closed.campaign_ids),
                keyword_count=len(closed.keyword_ids),
            ),
        )

    def _mark_job(self, segment_id: str, *, status: str) -> None:
        stamp = _iso(datetime.now(UTC))
        self.database.write(
            lambda connection: connection.execute(
                "UPDATE transcription_jobs SET status=?, attempts=attempts+1, worker_id=?,"
                " updated_at_utc=? WHERE segment_id=?",
                (status, self.worker_id, stamp, segment_id),
            )
        )

    def _mark_disposition(self, segment_id: str, *, retained: bool) -> None:
        """Tell the cleanup worker whether this audio is still needed.

        ``disposable`` is the common case: most segments match nothing, and
        marking them explicitly is what lets cleanup delete by *state* rather
        than by age alone.
        """
        stamp = _iso(datetime.now(UTC))
        disposition = "retained" if retained else "disposable"
        self.database.write(
            lambda connection: connection.execute(
                "UPDATE audio_segments SET disposition=?, updated_at_utc=? WHERE segment_id=?",
                (disposition, stamp, segment_id),
            )
        )

    # -- keyword index ---------------------------------------------------------

    def _matcher_for(self, station_id: str, version: int) -> KeywordMatcher | None:
        """One compiled matcher per (station, index version), cached.

        Cached because compiling an index of thousands of terms per segment
        would dominate the cost of transcribing one; keyed by version so a
        republished index is picked up on the next segment without a restart.
        """
        key = (station_id, version)
        cached = self._matchers.get(key)
        if cached is not None:
            return cached
        index = self.planner.keyword_index_for(station_id)
        if index is None:
            logger.warning(
                "No keyword index for station; segment cannot be matched",
                extra=log_fields(station_id=station_id),
            )
            return None
        matcher = KeywordMatcher(index)
        self._matchers = {key: matcher}  # Only the newest version is worth holding.
        return matcher


def _duration(milliseconds: int):
    from datetime import timedelta

    return timedelta(milliseconds=max(0, milliseconds))


def main() -> None:
    settings, database = bootstrap("transcription")
    try:
        TranscriptionWorker(settings, database).run()
    finally:
        database.close()


if __name__ == "__main__":
    main()


__all__ = ["TranscriptionWorker", "SegmentMissingError", "main"]
