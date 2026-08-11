"""Analysis worker: conversation -> local Qwen -> durable mention.

Consumes the analysis queue. One message is one conversation, so the model runs
exactly once per mention no matter how many campaigns it maps to.

The conversation is reloaded from SQLite rather than carried in the message:
transcripts travel by reference (ADR-003), which keeps messages small and means
the analysed text is the committed text, not a copy that might have drifted.

Analysis never fails a message. The LLM layer degrades to a deterministic
fallback, so a wedged model produces a thinner mention rather than a poison
message. Only genuinely retryable problems -- SQLite unavailable, a missing
conversation row that may still be committing -- leave the message for redelivery.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..observability import log_fields, trace_context
from ..pipeline.contracts import parse_analysis_job
from ..pipeline.errors import InvalidMessageError, RetryableError
from ..pipeline.factory import ANALYSIS_QUEUE, build_queue, build_s3_client
from ..pipeline.idempotency import MessageProcessor, ProcessingOutcome
from ..pipeline.queue import ReceivedMessage
from ..services.conversation_assembler import ClosedConversation, TranscribedSegment
from ..services.evidence import EvidenceClipService
from ..services.keyword_matcher import KeywordMatch
from ..services.llm_analysis import AnalysisRequest, ConversationAnalyzer
from ..services.result_writer import MentionContext, ResultWriter
from . import BaseWorker, bootstrap

logger = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class AnalysisWorker(BaseWorker):
    role = "analysis"

    def __init__(
        self,
        settings,
        database,
        *,
        queue=None,
        analyzer=None,
        result_writer=None,
        s3_client=None,
        evidence_service=None,
        **kwargs,
    ) -> None:
        super().__init__(settings, database, **kwargs)
        self.queue = queue or build_queue(settings, ANALYSIS_QUEUE)
        self.analyzer = analyzer or ConversationAnalyzer(settings)
        resolved_s3 = s3_client if s3_client is not None else _optional_s3(settings)
        self.writer = result_writer or ResultWriter(
            settings,
            database,
            s3_client=resolved_s3,
        )
        self.evidence = (
            evidence_service
            if evidence_service is not None
            else _optional_evidence(settings, database, resolved_s3)
        )
        # One attempt per FAILING mention per process: a clip that cannot be
        # built now (segments gone, FFmpeg fault) should not be retried every
        # idle tick. Successes need no entry, the evidence_available flag
        # already excludes them. A restart clears the set, which is a
        # deliberately cheap retry policy.
        self._evidence_failed: set[str] = set()
        self.processor = MessageProcessor(
            database=database,
            queue=self.queue,
            queue_name=ANALYSIS_QUEUE,
            component="analysis_worker",
            visibility_seconds=settings.RADIO_SQS_VISIBILITY_SECONDS,
            heartbeat_seconds=settings.RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS,
            max_processing_seconds=settings.RADIO_SQS_MAX_PROCESSING_SECONDS,
            max_messages=1,  # LLM work is serial on CPU; batching only adds latency.
            wait_seconds=settings.RADIO_SQS_WAIT_TIME_SECONDS,
        )
        self.stats = {
            "analysed": 0,
            "fallbacks": 0,
            "published": 0,
            "evidence": 0,
            "healed": 0,
        }
        # Same shape as the evidence memo: one retry per FAILING mention per
        # process, cleared on restart and when the cap is reached.
        self._analysis_retry_failed: set[str] = set()

    def tick(self) -> bool:
        result = self.processor.poll_once(self.handle)
        if result["received"] == 0:
            self._publish_backlog()
            self._evidence_backlog()
            self._analysis_backlog()
            return True
        return False

    # -- message handling ------------------------------------------------------

    def handle(self, message: ReceivedMessage) -> ProcessingOutcome:
        job = parse_analysis_job(message.body)
        with trace_context(job.trace_id):
            return self._handle_job(job, message)

    def _handle_job(self, job, message: ReceivedMessage) -> ProcessingOutcome:
        conversation = self._load_conversation(job)
        context = self._context_for(job, conversation)

        analysis = self.analyzer.analyze(
            AnalysisRequest(
                conversation_id=conversation.conversation_id,
                transcript=conversation.transcript_text,
                language=conversation.detected_language,
                content_type=context.content_type,  # type: ignore[arg-type]
                duration_ms=conversation.duration_ms,
                matched_keywords=tuple(
                    item.canonical_value for item in conversation.matches
                ),
                station_name=context.station_name,
            )
        )
        if analysis.status != "ready":
            self.stats["fallbacks"] += 1

        # The inbox row is committed with the mention, so a redelivery after the
        # SQS deduplication window expires is a no-op rather than a second
        # analysis of the same conversation.
        outcome = self.writer.persist(
            conversation,
            analysis,
            context,
            on_commit=lambda connection: self.processor.inbox.record_processed(
                connection, message, trace_id=job.trace_id
            ),
        )
        self.stats["analysed"] += 1

        # S3 publication happens outside the transaction and is allowed to fail:
        # SQLite already holds the record the API serves.
        if self.writer.publish(outcome.mention_id, conversation, analysis, context):
            self.stats["published"] += 1

        # Same posture for the audio clip: built after the mention is durable,
        # allowed to fail, retried by the idle-time backlog sweep.
        self._capture_evidence(outcome.mention_id)

        logger.info(
            "Mention analysed",
            extra=log_fields(
                mention_id=outcome.mention_id,
                conversation_id=conversation.conversation_id,
                station_id=conversation.station_id,
                trace_id=conversation.trace_id,
                analysis_status=analysis.status,
                campaign_rows=outcome.campaign_rows,
                included_campaign_rows=outcome.included_campaign_rows,
            ),
        )
        return ProcessingOutcome(handled=True, result_reference=outcome.mention_id)

    # -- reconstruction --------------------------------------------------------

    def _load_conversation(self, job) -> ClosedConversation:
        row = self.database.read_one(
            "SELECT * FROM conversation_sessions WHERE conversation_id=?",
            (job.conversation_id,),
        )
        if row is None:
            # Retryable, not permanent: the producing transaction may still be
            # committing, or this may be an out-of-order redelivery.
            raise RetryableError(
                "Conversation is not committed yet",
                detail=f"conversation_id={job.conversation_id}",
            )

        # Reconstruct from what the producer actually sent. Every value below
        # comes off the wire; the only fallbacks live inside MatchedKeywordRef
        # and apply solely to messages queued before those fields existed.
        #
        # Nothing here may default to exact/1.0/0: that is how an alias hit
        # became `exact` and a candidate became `confirmed` in the permanent
        # record.
        matches = tuple(
            KeywordMatch(
                keyword_id=item.keyword_id,
                campaign_ids=item.resolved_campaign_ids(job.campaign_ids),
                canonical_value=item.canonical_value,
                matched_text=item.matched_text,
                match_level=item.match_level,
                start_char=item.start_char,
                end_char=item.resolved_end_char,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                confidence=item.confidence,
            )
            for item in job.matched_keywords
        )
        if not matches:
            # An analysis job with no keywords cannot become a mention, and no
            # retry will add one.
            raise InvalidMessageError(
                "Analysis job carries no matched keywords",
                detail=f"conversation_id={job.conversation_id}",
            )

        started = _parse_time(row["started_at_utc"])
        ended = _parse_time(row["ended_at_utc"])
        transcript_id = str(job.transcript_reference.transcript_id)
        segment = TranscribedSegment(
            segment_id=transcript_id,
            station_id=job.station_id,
            station_session_id=str(row["station_session_id"] or ""),
            sequence_number=int(row["last_sequence_number"] or 0),
            transcript_id=transcript_id,
            text=str(row["transcript_text"] or ""),
            started_at=started,
            ended_at=ended,
            duration_ms=int(row["duration_ms"] or 0),
            content_class="speech",
            language=row["detected_language"],
        )
        try:
            missing = tuple(json.loads(str(row["missing_sequences_json"] or "[]")))
        except (TypeError, ValueError):
            missing = ()

        return ClosedConversation(
            conversation_id=job.conversation_id,
            station_id=job.station_id,
            station_session_id=str(row["station_session_id"] or ""),
            close_reason=str(row["close_reason"] or "silence"),  # type: ignore[arg-type]
            first_sequence_number=int(row["first_sequence_number"] or 0),
            last_sequence_number=int(row["last_sequence_number"] or 0),
            started_at=started,
            ended_at=ended,
            duration_ms=int(row["duration_ms"] or 0),
            transcript_text=str(row["transcript_text"] or ""),
            detected_language=row["detected_language"],
            segments=(segment,),
            matches=matches,
            missing_sequences=missing,
            trace_id=str(row["trace_id"] or job.trace_id),
        )

    def _context_for(self, job, conversation: ClosedConversation) -> MentionContext:
        row = self.database.read_one(
            "SELECT content_type, content_type_confidence FROM conversation_sessions"
            " WHERE conversation_id=?",
            (job.conversation_id,),
        )
        station = self.database.read_one(
            "SELECT display_name FROM station_subscriptions WHERE station_id=?",
            (job.station_id,),
        )
        return MentionContext(
            station_name=str(station["display_name"]) if station else "",
            content_type=str(row["content_type"]) if row else "unknown",
            content_confidence=float(row["content_type_confidence"] or 0.0) if row else 0.0,
            campaign_policies=self._campaign_policies(job.campaign_ids),
            transcript_id=str(job.transcript_reference.transcript_id),
        )

    def _campaign_policies(self, campaign_ids: list[str]) -> dict[str, dict[str, bool]]:
        if not campaign_ids:
            return {}
        # `placeholders` is built from the COUNT of ids only -- it can never be
        # anything but "?,?,?". Every campaign id travels as a bound parameter,
        # so no caller-supplied text reaches the SQL text. The id count is
        # bounded by MAX_CAMPAIGN_IDS in the message contract, which is what
        # keeps the statement from growing without limit.
        placeholders = ",".join("?" for _ in campaign_ids)
        sql = (
            "SELECT campaign_id, policy_json"  # nosec B608 (only '?' is interpolated)
            " FROM campaign_content_policies"
            f" WHERE campaign_id IN ({placeholders})"
        )
        rows = self.database.read_all(sql, tuple(campaign_ids))
        policies: dict[str, dict[str, bool]] = {}
        for row in rows:
            try:
                policies[str(row["campaign_id"])] = {
                    str(key): bool(value)
                    for key, value in json.loads(str(row["policy_json"])).items()
                }
            except (TypeError, ValueError):
                continue
        return policies

    # -- evidence --------------------------------------------------------------

    #: Captures per idle sweep. Each one may spend real time in FFmpeg and S3,
    #: and the sweep runs inside a tick, so the batch stays small to keep
    #: heartbeats fresh and shutdown prompt.
    EVIDENCE_SWEEP_BATCH = 3

    #: Bound on the per-process failure memo. Clearing it simply re-allows one
    #: retry of old failures, which capture() makes safe.
    EVIDENCE_FAILED_MEMO_LIMIT = 5000

    def _capture_evidence(self, mention_id: str) -> bool:
        """Attach the audio clip for one mention; failure never loses it."""
        if self.evidence is None or mention_id in self._evidence_failed:
            return False
        try:
            if self.evidence.capture(mention_id):
                self.stats["evidence"] += 1
                return True
        except Exception as error:  # noqa: BLE001 - the mention outlives its clip
            if len(self._evidence_failed) >= self.EVIDENCE_FAILED_MEMO_LIMIT:
                self._evidence_failed.clear()
            self._evidence_failed.add(mention_id)
            logger.warning(
                "Evidence clip capture failed",
                extra=log_fields(mention_id=mention_id, error=str(error)[:300]),
            )
        return False

    def _evidence_backlog(self) -> None:
        """Give clip-less mentions their audio during idle time.

        This is also the backfill: mentions created before evidence capture
        existed satisfy the same query and get clips as soon as a deployed
        worker has an idle tick, so no operator command is involved.

        Pages past known failures instead of re-reading only the newest rows:
        a head of permanently un-clippable mentions must not starve every
        older mention behind it. Work is bounded per sweep and the loop backs
        off between captures so heartbeats stay fresh and stop stays prompt.
        """
        if self.evidence is None:
            return
        captured = 0
        offset = 0
        page_size = 25
        while captured < self.EVIDENCE_SWEEP_BATCH and not self.should_stop:
            rows = self.database.read_all(
                "SELECT mention_id FROM mention_events WHERE evidence_available=0"
                " ORDER BY created_at_utc DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
            if not rows:
                return
            for row in rows:
                if self.should_stop:
                    return
                mention_id = str(row["mention_id"])
                if mention_id in self._evidence_failed:
                    continue
                if self._capture_evidence(mention_id):
                    captured += 1
                    self.beat(status="ok", detail={"evidence_backfill": captured})
                    if captured >= self.EVIDENCE_SWEEP_BATCH:
                        return
            if len(rows) < page_size:
                return
            offset += page_size

    #: Fallback re-analyses per idle sweep. Each one is a full model call, so
    #: the batch stays small; convergence comes from repetition, not size.
    ANALYSIS_RETRY_BATCH = 2

    def _analysis_backlog(self) -> None:
        """Heal mentions that fell back while the model server was down.

        A mention analysed during an outage carries a fallback row (neutral,
        zero confidence, transcript excerpt) and nothing used to revisit it:
        the outage became permanent grey mentions. During idle time, when the
        server answers its health probe, this sweep re-runs the newest
        fallbacks and replaces their rows with real analyses. It stops at the
        first non-ready result, because that means the server is unhealthy
        again and further attempts this tick would only churn.
        """
        probe = getattr(self.analyzer, "healthy", None)
        if not callable(probe) or not probe():
            return
        healed = 0
        offset = 0
        page_size = 25
        while healed < self.ANALYSIS_RETRY_BATCH and not self.should_stop:
            rows = self.database.read_all(
                "SELECT ar.mention_id, e.conversation_id, e.station_name"
                " FROM analysis_results ar"
                " JOIN mention_events e ON e.mention_id = ar.mention_id"
                " WHERE ar.status = 'fallback'"
                " ORDER BY e.created_at_utc DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
            if not rows:
                return
            for row in rows:
                if self.should_stop:
                    return
                mention_id = str(row["mention_id"])
                if mention_id in self._analysis_retry_failed:
                    continue
                outcome = self._retry_analysis(
                    mention_id,
                    str(row["conversation_id"] or ""),
                    str(row["station_name"] or ""),
                )
                if outcome is False:
                    # The server just failed a real request; stop the sweep
                    # rather than burning the batch against a dead model.
                    return
                if outcome:
                    healed += 1
                    self.stats["healed"] += 1
                    self.beat(status="ok", detail={"analysis_healed": healed})
                    if healed >= self.ANALYSIS_RETRY_BATCH:
                        return
            if len(rows) < page_size:
                return
            offset += page_size

    def _retry_analysis(
        self, mention_id: str, conversation_id: str, station_name: str
    ) -> bool | None:
        """One re-analysis.

        True: the row was replaced with a real result. None: this mention can
        never heal (no transcript) and the sweep should move on. False: the
        model failed a real request and the sweep should stop.
        """
        if len(self._analysis_retry_failed) >= self.EVIDENCE_FAILED_MEMO_LIMIT:
            self._analysis_retry_failed.clear()
        conversation = self.database.read_one(
            "SELECT transcript_text, detected_language, content_type, duration_ms"
            " FROM conversation_sessions WHERE conversation_id=?",
            (conversation_id,),
        )
        transcript = str(conversation["transcript_text"] or "").strip() if conversation else ""
        if not transcript:
            self._analysis_retry_failed.add(mention_id)
            return None
        keywords = self.database.read_all(
            "SELECT DISTINCT canonical_value FROM mention_keywords WHERE mention_id=?",
            (mention_id,),
        )
        result = self.analyzer.analyze(
            AnalysisRequest(
                conversation_id=conversation_id,
                transcript=transcript,
                language=conversation["detected_language"],
                content_type=str(conversation["content_type"] or "unknown"),  # type: ignore[arg-type]
                duration_ms=int(conversation["duration_ms"] or 0),
                matched_keywords=tuple(
                    str(item["canonical_value"]) for item in keywords
                ),
                station_name=station_name,
            )
        )
        if result.status != "ready":
            self._analysis_retry_failed.add(mention_id)
            return False
        self.writer.refresh_analysis(mention_id, result)
        logger.info(
            "Fallback analysis healed",
            extra=log_fields(mention_id=mention_id, sentiment=result.sentiment),
        )
        return True

    # -- recovery --------------------------------------------------------------

    def _publish_backlog(self) -> None:
        """Finish S3 exports left over from a crash between the two writes."""
        pending = self.writer.unpublished_mentions(limit=10)
        if not pending:
            return
        logger.info("Retrying unpublished mention exports", extra=log_fields(pending=len(pending)))


def _optional_s3(settings):
    """An S3 client when one is configured, else None.

    A single-node deployment can run entirely on SQLite; publication is an
    export step, not a prerequisite for a mention to exist.
    """
    if not settings.RADIO_S3_BUCKET.strip():
        return None
    try:
        return build_s3_client(settings)
    except Exception:  # noqa: BLE001 - absent credentials must not stop analysis
        logger.warning("No S3 client available; mentions will stay local until exported")
        return None


def _optional_evidence(settings, database, s3_client):
    """An evidence clip service when its dependencies exist, else None.

    Needs both an S3 client (the clip destination) and a readable segment
    store (the clip source). Either being absent disables capture without
    touching analysis itself.
    """
    if s3_client is None:
        return None
    try:
        from ..pipeline.factory import build_segment_store

        store = build_segment_store(settings)
    except Exception:  # noqa: BLE001 - a broken store must not stop analysis
        logger.warning("Segment store unavailable; mentions will have no audio clips")
        return None
    return EvidenceClipService(settings, database, store, s3_client)


def main() -> None:
    settings, database = bootstrap("analysis")
    try:
        AnalysisWorker(settings, database).run()
    finally:
        database.close()


if __name__ == "__main__":
    main()


__all__ = ["AnalysisWorker", "main"]
