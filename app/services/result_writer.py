"""Durable results: SQLite mappings first, S3 documents second (ADR-009 §4).

The fan-out this module exists for
----------------------------------
One physical conversation produces exactly **one** ``mention_events`` row, one
transcript and one analysis, and **many** mapping rows:

* one ``mention_campaigns`` row per campaign that tracked a matched keyword;
* one ``mention_keywords`` row per keyword that matched.

``mention_events`` deliberately has no ``campaign_id`` and no ``keyword_id``
column. Attribution lives only in the mapping tables, which makes "transcribe
once, analyse once, attribute many times" true by construction rather than by
discipline.

Ordering and consistency
------------------------
SQLite is committed first and S3 second, never the reverse. SQLite is the
system of record the API reads; an S3 object with no row behind it is invisible
to every consumer, whereas a row with no object is a visible, retryable
inconsistency. Publication state is tracked on the row, so a crash between the
two leaves work that a later pass finishes rather than data that is silently
half-written.

Every write is idempotent. ``mention_events.conversation_id`` is UNIQUE, so a
redelivered analysis job re-derives the same mention rather than creating a
second one, and S3 keys are deterministic so a repeated publish overwrites
identical bytes instead of accumulating duplicates.

Campaign content policy is applied **here**, not in the matcher. The physical
event is recorded once with its evidence; each campaign's row then records
whether that campaign includes this content type and, if not, why. A song
containing "Amazon" therefore leaves an auditable excluded row rather than
vanishing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from ..observability import log_fields
from ..pipeline.ids import new_id
from .content_classifier import is_included
from .conversation_assembler import ClosedConversation
from .llm_analysis import AnalysisResult

logger = logging.getLogger(__name__)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WriteOutcome:
    """What one persist call did, in assertable numbers."""

    mention_id: str
    created: bool
    campaign_rows: int
    included_campaign_rows: int
    keyword_rows: int
    result_s3_key: str | None = None
    published: bool = False

    @property
    def excluded_campaign_rows(self) -> int:
        return self.campaign_rows - self.included_campaign_rows


@dataclass
class MentionContext:
    """Everything about a conversation that is not in the conversation itself."""

    station_name: str = ""
    content_type: str = "unknown"
    content_confidence: float = 0.0
    #: ``{campaign_id: {policy flag: bool}}``; missing campaigns use defaults.
    campaign_policies: dict[str, dict[str, bool]] = field(default_factory=dict)
    evidence_storage_key: str | None = None
    transcript_id: str | None = None


class ResultWriter:
    """Commits mentions and publishes their documents."""

    def __init__(
        self,
        settings: Settings,
        database: Any,
        *,
        s3_client: Any | None = None,
        clock=None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._s3 = s3_client
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- SQLite ---------------------------------------------------------------

    def persist(
        self,
        conversation: ClosedConversation,
        analysis: AnalysisResult,
        context: MentionContext | None = None,
        *,
        on_commit: Callable[[sqlite3.Connection], None] | None = None,
    ) -> WriteOutcome:
        """Commit the mention and all of its mappings in one short transaction.

        Short on purpose: no network, no S3, no model call happens inside it
        (ADR-004 §3). One slow transaction stalls every writer in the process.

        ``on_commit`` runs inside that same transaction. The analysis worker
        uses it to write its inbox row, so acknowledging the message and
        recording the mention either both happen or neither does.
        """
        ctx = context or MentionContext()
        now = self._clock()
        stamp = _iso(now)
        mention_id = new_id()

        def write(connection: sqlite3.Connection) -> WriteOutcome:
            connection.execute(
                """
                INSERT INTO conversation_sessions(
                  conversation_id, station_id, station_session_id, state, close_reason,
                  first_sequence_number, last_sequence_number, started_at_utc, ended_at_utc,
                  duration_ms, transcript_text, detected_language, content_type,
                  content_type_confidence, missing_sequences_json, trace_id,
                  created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  state='closed',
                  close_reason=excluded.close_reason,
                  transcript_text=excluded.transcript_text,
                  content_type=excluded.content_type,
                  content_type_confidence=excluded.content_type_confidence,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (
                    conversation.conversation_id,
                    conversation.station_id,
                    conversation.station_session_id,
                    conversation.close_reason,
                    conversation.first_sequence_number,
                    conversation.last_sequence_number,
                    _iso(conversation.started_at),
                    _iso(conversation.ended_at),
                    conversation.duration_ms,
                    conversation.transcript_text,
                    conversation.detected_language,
                    ctx.content_type,
                    ctx.content_confidence,
                    json.dumps(list(conversation.missing_sequences)),
                    conversation.trace_id,
                    stamp,
                    stamp,
                ),
            )

            # UNIQUE(conversation_id) is what makes redelivery safe: a second
            # attempt re-derives the same mention instead of forking a new one.
            cursor = connection.execute(
                """
                INSERT INTO mention_events(
                  mention_id, conversation_id, station_id, station_name, content_type,
                  detected_language, broadcast_start_utc, broadcast_end_utc,
                  transcript_id, evidence_storage_key, evidence_available,
                  trace_id, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO NOTHING
                """,
                (
                    mention_id,
                    conversation.conversation_id,
                    conversation.station_id,
                    ctx.station_name,
                    ctx.content_type,
                    conversation.detected_language,
                    _iso(conversation.started_at),
                    _iso(conversation.ended_at),
                    ctx.transcript_id,
                    ctx.evidence_storage_key,
                    1 if ctx.evidence_storage_key else 0,
                    conversation.trace_id,
                    stamp,
                    stamp,
                ),
            )
            created = bool(cursor.rowcount)
            row = connection.execute(
                "SELECT mention_id, result_s3_key FROM mention_events WHERE conversation_id=?",
                (conversation.conversation_id,),
            ).fetchone()
            resolved_id = str(row["mention_id"])
            existing_key = row["result_s3_key"]

            campaign_rows = self._write_campaign_rows(connection, conversation, ctx, resolved_id, stamp)
            keyword_rows = self._write_keyword_rows(connection, conversation, resolved_id, stamp)
            self._write_analysis(connection, resolved_id, analysis, stamp)
            if on_commit is not None:
                on_commit(connection)

            return WriteOutcome(
                mention_id=resolved_id,
                created=created,
                campaign_rows=campaign_rows[0],
                included_campaign_rows=campaign_rows[1],
                keyword_rows=keyword_rows,
                result_s3_key=existing_key,
                published=bool(existing_key),
            )

        outcome = self._database.write(write)
        logger.info(
            "Mention persisted",
            extra=log_fields(
                mention_id=outcome.mention_id,
                conversation_id=conversation.conversation_id,
                station_id=conversation.station_id,
                trace_id=conversation.trace_id,
                created=outcome.created,
                campaign_rows=outcome.campaign_rows,
                included_campaign_rows=outcome.included_campaign_rows,
                keyword_rows=outcome.keyword_rows,
            ),
        )
        return outcome

    def _write_campaign_rows(
        self,
        connection: sqlite3.Connection,
        conversation: ClosedConversation,
        context: MentionContext,
        mention_id: str,
        stamp: str,
    ) -> tuple[int, int]:
        """One row per campaign, carrying its own include/exclude verdict."""
        defaults = self._settings.content_policy_defaults
        total = 0
        included_count = 0
        for campaign_id in conversation.campaign_ids:
            policy = {**defaults, **context.campaign_policies.get(campaign_id, {})}
            included, reason = is_included(context.content_type, policy)
            connection.execute(
                """
                INSERT INTO mention_campaigns(
                  mention_id, campaign_id, included, exclusion_reason, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mention_id, campaign_id) DO UPDATE SET
                  included=excluded.included,
                  exclusion_reason=excluded.exclusion_reason
                """,
                (mention_id, campaign_id, 1 if included else 0, reason, stamp),
            )
            total += 1
            included_count += 1 if included else 0
        return total, included_count

    @staticmethod
    def _write_keyword_rows(
        connection: sqlite3.Connection,
        conversation: ClosedConversation,
        mention_id: str,
        stamp: str,
    ) -> int:
        for match in conversation.matches:
            connection.execute(
                """
                INSERT INTO mention_keywords(
                  mention_id, keyword_id, campaign_id, canonical_value, matched_text,
                  match_level, confirmed, start_ms, end_ms, start_char, end_char,
                  confidence, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mention_id, keyword_id) DO UPDATE SET
                  matched_text=excluded.matched_text,
                  match_level=excluded.match_level,
                  confirmed=excluded.confirmed,
                  confidence=excluded.confidence
                """,
                (
                    mention_id,
                    match.keyword_id,
                    match.campaign_ids[0] if match.campaign_ids else "",
                    match.canonical_value,
                    match.matched_text,
                    match.match_level,
                    0 if match.requires_confirmation else 1,
                    match.start_ms or 0,
                    match.end_ms or 0,
                    match.start_char,
                    match.end_char,
                    match.confidence,
                    stamp,
                ),
            )
        return len(conversation.matches)

    @staticmethod
    def _write_analysis(
        connection: sqlite3.Connection,
        mention_id: str,
        analysis: AnalysisResult,
        stamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO analysis_results(
              mention_id, analysis_job_id, schema_version, status, model, content_type,
              language, relevant, summary, translated_summary, main_topic, sentiment,
              speaker_stance, urgency, entities_json, key_points_json, evidence_json,
              confidence, needs_review, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mention_id) DO UPDATE SET
              status=excluded.status,
              model=excluded.model,
              summary=excluded.summary,
              translated_summary=excluded.translated_summary,
              main_topic=excluded.main_topic,
              sentiment=excluded.sentiment,
              speaker_stance=excluded.speaker_stance,
              urgency=excluded.urgency,
              entities_json=excluded.entities_json,
              key_points_json=excluded.key_points_json,
              evidence_json=excluded.evidence_json,
              confidence=excluded.confidence,
              needs_review=excluded.needs_review,
              updated_at_utc=excluded.updated_at_utc
            """,
            (
                mention_id,
                new_id(),
                analysis.schema_version,
                analysis.status,
                analysis.model,
                analysis.content_type,
                analysis.language,
                1 if analysis.relevant else 0,
                analysis.summary,
                analysis.translated_summary,
                analysis.main_topic,
                analysis.sentiment,
                analysis.speaker_stance,
                analysis.urgency,
                json.dumps([entity.model_dump() for entity in analysis.entities], ensure_ascii=False),
                json.dumps(analysis.key_points, ensure_ascii=False),
                json.dumps([item.model_dump() for item in analysis.evidence], ensure_ascii=False),
                analysis.confidence,
                1 if analysis.needs_review else 0,
                stamp,
                stamp,
            ),
        )

    # -- S3 -------------------------------------------------------------------

    def publish(
        self,
        mention_id: str,
        conversation: ClosedConversation,
        analysis: AnalysisResult,
        context: MentionContext | None = None,
    ) -> str | None:
        """Write the mention's documents to S3 and record the key.

        Runs *outside* any transaction. Returns the metadata key, or None when
        no S3 client is configured (single-node deployments may keep everything
        local until a result is exported).
        """
        if self._s3 is None:
            return None
        ctx = context or MentionContext()
        prefix = self._mention_prefix(mention_id, conversation.started_at)

        documents = {
            f"{prefix}metadata.json": self._metadata_document(mention_id, conversation, ctx),
            f"{prefix}transcript.json": self._transcript_document(conversation),
            f"{prefix}analysis.json": analysis.as_payload(),
        }
        try:
            for key, document in documents.items():
                self._put_json(key, document)
        except Exception as error:  # noqa: BLE001 - recorded, retried by a later pass
            logger.warning(
                "Publishing mention documents failed; SQLite already holds the record",
                extra=log_fields(
                    mention_id=mention_id,
                    error_type=type(error).__name__,
                ),
            )
            self._record_publish_failure(mention_id, error)
            return None

        metadata_key = f"{prefix}metadata.json"
        stamp = _iso(self._clock())
        self._database.write(
            lambda connection: connection.execute(
                "UPDATE mention_events SET result_s3_key=?, updated_at_utc=? WHERE mention_id=?",
                (metadata_key, stamp, mention_id),
            )
        )
        return metadata_key

    def _mention_prefix(self, mention_id: str, broadcast_at: datetime) -> str:
        moment = broadcast_at.astimezone(UTC)
        # Deterministic and date-partitioned: republishing overwrites identical
        # bytes, and no consumer ever has to list the whole bucket.
        return (
            f"{self._settings.RADIO_MENTIONS_PREFIX}"
            f"{moment:%Y/%m/%d}/{mention_id}/"
        )

    def _put_json(self, key: str, document: dict[str, Any]) -> None:
        body = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._s3.put_object(
            Bucket=self._settings.RADIO_S3_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            # Encryption at rest is not optional for broadcast content, and no
            # ACL is ever set: bucket policy is the only access path.
            ServerSideEncryption="AES256",
        )

    @staticmethod
    def _metadata_document(
        mention_id: str, conversation: ClosedConversation, context: MentionContext
    ) -> dict[str, Any]:
        return {
            "schema": "radio.mention.v1",
            "mention_id": mention_id,
            "conversation_id": conversation.conversation_id,
            "station_id": conversation.station_id,
            "station_name": context.station_name,
            "content_type": context.content_type,
            "detected_language": conversation.detected_language,
            "broadcast_start_utc": _iso(conversation.started_at),
            "broadcast_end_utc": _iso(conversation.ended_at),
            "duration_ms": conversation.duration_ms,
            "close_reason": conversation.close_reason,
            "campaign_ids": list(conversation.campaign_ids),
            "keyword_ids": list(conversation.keyword_ids),
            # Object keys, never presigned URLs: a URL in a stored document is
            # a credential with an expiry nobody tracks.
            "evidence_storage_key": context.evidence_storage_key,
            "trace_id": conversation.trace_id,
        }

    @staticmethod
    def _transcript_document(conversation: ClosedConversation) -> dict[str, Any]:
        return {
            "schema": "radio.transcript.v1",
            "conversation_id": conversation.conversation_id,
            "station_id": conversation.station_id,
            # The original-language transcript is the evidence and is retained
            # verbatim; nothing here is a translation.
            "text": conversation.transcript_text,
            "detected_language": conversation.detected_language,
            "started_at_utc": _iso(conversation.started_at),
            "ended_at_utc": _iso(conversation.ended_at),
            "missing_sequences": list(conversation.missing_sequences),
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "transcript_id": segment.transcript_id,
                    "sequence_number": segment.sequence_number,
                    "text": segment.text,
                    "started_at_utc": _iso(segment.started_at),
                    "ended_at_utc": _iso(segment.ended_at),
                    "content_class": segment.content_class,
                    "language": segment.language,
                }
                for segment in conversation.segments
            ],
            "matches": [
                {
                    "keyword_id": match.keyword_id,
                    "canonical_value": match.canonical_value,
                    "matched_text": match.matched_text,
                    "match_level": match.match_level,
                    "start_char": match.start_char,
                    "end_char": match.end_char,
                    "start_ms": match.start_ms,
                    "end_ms": match.end_ms,
                    "campaign_ids": list(match.campaign_ids),
                }
                for match in conversation.matches
            ],
        }

    def _record_publish_failure(self, mention_id: str, error: Exception) -> None:
        stamp = _iso(self._clock())

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO processing_failures(
                  component, error_code, retryable, mention_id, message, detail, created_at_utc
                ) VALUES ('result_writer', 's3_publish_failed', 1, ?, ?, ?, ?)
                """,
                (
                    mention_id,
                    "Failed to publish mention documents to S3",
                    f"{type(error).__name__}: {error}"[:2000],
                    stamp,
                ),
            )

        try:
            self._database.write(write)
        except Exception:  # noqa: BLE001 - bookkeeping must never mask the failure
            logger.exception("Could not record a publish failure")

    def unpublished_mentions(self, *, limit: int = 50) -> list[str]:
        """Mentions committed to SQLite but not yet exported.

        This is the recovery path for a crash between the two writes: the row
        exists, the objects do not, and a later pass finishes the job.
        """
        rows = self._database.read_all(
            "SELECT mention_id FROM mention_events WHERE result_s3_key IS NULL"
            " ORDER BY created_at_utc LIMIT ?",
            (limit,),
        )
        return [str(row["mention_id"]) for row in rows]


__all__ = ["MentionContext", "ResultWriter", "WriteOutcome"]
