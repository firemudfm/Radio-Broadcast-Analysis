"""Listener worker: shared station sessions -> spool -> transcription queue.

Owns the async :class:`~app.services.stream_supervisor.StreamSupervisor` and the
durable side of what it produces. For each retained segment:

1. encode the PCM (Opus, or lossless WAV if the encoder is unavailable);
2. write it to the segment store, which fsyncs and renames atomically;
3. in ONE SQLite transaction, insert the ``audio_segments`` row, the
   ``transcription_jobs`` row and the outbox event.

Step 3 is the whole reliability argument. Bytes land on disk before anything
references them, and the reference plus the intent-to-send are committed
together -- so there is no window in which a job exists without its audio, and
none in which a segment is durably recorded but never queued (ADR-009).

The disk watermark is checked before admitting a segment, not after writing it:
back-pressure that only triggers after the write is back-pressure that already
filled the disk.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import UTC, datetime

from ..observability import log_fields, trace_context
from ..pipeline import outbox
from ..pipeline.contracts import StorageDescriptor, TranscriptionJobV1
from ..pipeline.enums import SpoolPressure
from ..pipeline.factory import TRANSCRIPTION_QUEUE, build_segment_store
from ..pipeline.ids import new_id
from ..pipeline.segment_store import SegmentRef
from ..services.audio_classifier import build_classifier
from ..services.keyword_index import language_hints_for
from ..services.segment_encoder import SegmentEncoder
from ..services.stream_supervisor import (
    SegmentEvent,
    SessionStatus,
    StationPlan,
    StreamSupervisor,
)
from ..services.subscription_planner import SubscriptionPlanner
from . import BaseWorker, bootstrap

logger = logging.getLogger(__name__)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ListenerWorker(BaseWorker):
    """Supervises the stations assigned to this shard."""

    role = "listener"

    def __init__(self, settings, database, *, segment_store=None, **kwargs) -> None:
        super().__init__(settings, database, **kwargs)
        self.planner = SubscriptionPlanner(settings, database)
        self.store = segment_store or build_segment_store(settings)
        self.encoder = SegmentEncoder(
            sample_rate=settings.RADIO_SAMPLE_RATE,
            bitrate=settings.RADIO_SEGMENT_OPUS_BITRATE,
            ffmpeg_binary=settings.RADIO_LISTENER_FFMPEG_BINARY,
        )
        self.supervisor = StreamSupervisor(
            settings,
            classifier_factory=lambda: build_classifier(settings),
            emit=self.handle_segment,
            on_status=self.handle_status,
        )
        self._pressure: SpoolPressure = "ok"
        self._admitted = 0
        self._rejected_by_pressure = 0
        # Stations warned about once already. Bounded by the number of stations
        # this shard owns, and cleared per station the moment one is resolved.
        self._unresolved: set[str] = set()

    # -- async entry point -----------------------------------------------------

    def run(self) -> None:  # type: ignore[override]
        """The listener is async, so it replaces the synchronous base loop."""
        self.install_signal_handlers()
        logger.info(
            "Listener starting",
            extra=log_fields(
                worker_id=self.worker_id,
                shard_index=self.shard_index,
                shard_count=self.settings.RADIO_LISTENER_SHARD_COUNT,
            ),
        )
        try:
            asyncio.run(self._run_async())
        finally:
            with contextlib.suppress(Exception):
                self.heartbeat.stop()

    async def _run_async(self) -> None:
        self.beat(status="starting")
        try:
            while not self.should_stop:
                try:
                    await self._reconcile()
                except Exception:  # noqa: BLE001 - a bad cycle must not end capture
                    logger.exception(
                        "Listener reconcile failed", extra=log_fields(worker_id=self.worker_id)
                    )
                    self.beat(status="degraded")
                await asyncio.sleep(self.settings.RADIO_PLANNER_POLL_SECONDS)
        finally:
            await self.supervisor.shutdown()

    async def _reconcile(self) -> None:
        self._pressure = self._spool_pressure()
        plans = [] if self._pressure == "emergency" else self._assigned_plans()
        result = await self.supervisor.reconcile(plans)
        self.beat(
            status="degraded" if self._pressure in {"pause", "emergency"} else "ok",
            detail={
                **result,
                "spool_pressure": self._pressure,
                "spool_usage_percent": self._usage_percent(),
                "segments_admitted": self._admitted,
                "segments_rejected_by_pressure": self._rejected_by_pressure,
                "sessions": self.supervisor.status_snapshot(),
            },
        )

    def _assigned_plans(self) -> list[StationPlan]:
        """Stations this shard owns, with their current index version."""
        plans: list[StationPlan] = []
        for row in self.planner.assigned_stations(shard_index=self.shard_index):
            station_id = str(row.get("station_id"))
            url = str(row.get("stream_url") or "").strip()
            if not url:
                # A subscription with no resolved stream URL is a catalogue gap,
                # not a listener failure. Say so ONCE per station: the planner
                # reconciles every few seconds, so warning on each pass buried
                # the logs in thousands of copies of the same line and told
                # nobody anything they did not know after the first one.
                if station_id not in self._unresolved:
                    self._unresolved.add(station_id)
                    logger.warning(
                        "Station has no stream URL yet; the planner resolves it, skipping for now",
                        extra=log_fields(
                            station_id=station_id,
                            last_error=str(row.get("last_error") or ""),
                        ),
                    )
                continue
            if station_id in self._unresolved:
                self._unresolved.discard(station_id)
                logger.info(
                    "Station now has a stream URL; starting it",
                    extra=log_fields(station_id=station_id),
                )
            plans.append(
                StationPlan(
                    station_id=str(row["station_id"]),
                    stream_url=url,
                    display_name=str(row.get("display_name") or ""),
                    keyword_index_version=int(row.get("keyword_index_version") or 0),
                )
            )
        return plans

    # -- segment handling ------------------------------------------------------

    async def handle_segment(self, event: SegmentEvent) -> None:
        """Persist one retained segment and queue it for transcription."""
        if self._pressure in {"pause", "emergency"}:
            # Refuse before spending CPU on encoding or bytes on disk.
            self._rejected_by_pressure += 1
            logger.warning(
                "Dropping a segment because the spool is above its watermark",
                extra=log_fields(
                    station_id=event.station_id,
                    spool_pressure=self._pressure,
                    spool_usage_percent=self._usage_percent(),
                ),
            )
            return

        with trace_context(event.trace_id):
            try:
                # Encoding and the store write are blocking; keeping them off
                # the event loop is what stops one station's disk I/O from
                # stalling every other station's reads.
                await asyncio.to_thread(self._persist_segment, event)
                self._admitted += 1
            except Exception:  # noqa: BLE001 - one lost segment is not a dead station
                logger.exception(
                    "Could not persist a segment",
                    extra=log_fields(
                        station_id=event.station_id, trace_id=event.trace_id
                    ),
                )

    def _persist_segment(self, event: SegmentEvent) -> None:
        segment_id = new_id()
        encoded, extension = self.encoder.encode(event.pcm)
        ref = SegmentRef(
            station_id=event.station_id, segment_id=segment_id, extension=extension
        )
        # Bytes first: a job row must never reference audio that is not on disk.
        descriptor = self.store.write(ref, encoded)

        # Everything from here on is covered by the rollback below. Nothing yet
        # references these bytes, and the cleanup worker sweeps `audio_segments`
        # rows -- so a file with no row is invisible to it and would leak for the
        # life of the spool. Contract validation lives inside the guard for
        # exactly that reason: a rejected message must not cost a stranded file.
        try:
            job_id = new_id()
            job = TranscriptionJobV1(
                job_id=job_id,
                segment_id=segment_id,
                station_id=event.station_id,
                station_session_id=event.station_session_id,
                sequence_number=event.sequence_number,
                started_at=event.started_at,
                duration_ms=event.duration_ms,
                content_class=event.content_class,
                language_hints=self._language_hints(event.station_id),
                keyword_index_version=event.keyword_index_version,
                storage=descriptor,
                trace_id=event.trace_id,
                created_at=datetime.now(UTC),
            )
            body = job.to_body()

            def write(connection: sqlite3.Connection) -> None:
                self._insert_segment(connection, event, segment_id, descriptor)
                self._insert_job(connection, event, segment_id, job_id)
                # Same transaction as the rows above: this is the guarantee that
                # a durable segment is always a queued segment.
                outbox.enqueue(
                    connection,
                    queue_name=TRANSCRIPTION_QUEUE,
                    message_group_id=job.message_group_id(),
                    message_deduplication_id=job.deduplication_id(),
                    payload=body,
                    trace_id=event.trace_id,
                )

            self.database.write(write)
        except Exception:
            with contextlib.suppress(Exception):
                self.store.delete(descriptor)
            raise

    def _insert_segment(
        self,
        connection: sqlite3.Connection,
        event: SegmentEvent,
        segment_id: str,
        descriptor: StorageDescriptor,
    ) -> None:
        import json

        stamp = _iso(datetime.now(UTC))
        connection.execute(
            """
            INSERT INTO audio_segments(
              segment_id, station_id, station_session_id, sequence_number,
              started_at_utc, ended_at_utc, duration_ms, content_class,
              content_class_confidence, classifier_signals_json, storage_backend,
              storage_path, storage_bucket, storage_key, sha256, size_bytes,
              disposition, keyword_index_version, trace_id, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(station_session_id, sequence_number) DO NOTHING
            """,
            (
                segment_id,
                event.station_id,
                event.station_session_id,
                event.sequence_number,
                _iso(event.started_at),
                _iso(event.ended_at),
                event.duration_ms,
                event.content_class,
                event.content_class_confidence,
                json.dumps(event.classifier_signals, default=str),
                descriptor.backend,
                descriptor.path,
                descriptor.bucket,
                descriptor.key,
                descriptor.sha256,
                descriptor.size_bytes,
                event.keyword_index_version,
                event.trace_id,
                stamp,
                stamp,
            ),
        )

    @staticmethod
    def _insert_job(
        connection: sqlite3.Connection,
        event: SegmentEvent,
        segment_id: str,
        job_id: str,
    ) -> None:
        stamp = _iso(datetime.now(UTC))
        connection.execute(
            """
            INSERT INTO transcription_jobs(
              transcription_job_id, segment_id, station_id, status, attempts,
              trace_id, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
            ON CONFLICT(segment_id) DO NOTHING
            """,
            (job_id, segment_id, event.station_id, event.trace_id, stamp, stamp),
        )

    def _language_hints(self, station_id: str) -> list[str]:
        index = self.planner.keyword_index_for(station_id)
        if index is None:
            return []
        row = self.database.read_one(
            "SELECT language_codes_json FROM station_subscriptions WHERE station_id=?",
            (station_id,),
        )
        import json

        try:
            station_languages = json.loads(str(row["language_codes_json"])) if row else []
        except (TypeError, ValueError):
            station_languages = []
        return language_hints_for(index, list(station_languages))

    # -- status and back-pressure ---------------------------------------------

    async def handle_status(self, status: SessionStatus) -> None:
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO station_sessions(
                  station_session_id, station_id, generation, shard_index, worker_id,
                  sample_rate, status, last_audio_at_utc, last_error, started_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_session_id) DO UPDATE SET
                  generation=excluded.generation,
                  status=excluded.status,
                  last_audio_at_utc=excluded.last_audio_at_utc,
                  last_error=excluded.last_error
                """,
                (
                    status.station_session_id,
                    status.station_id,
                    status.generation,
                    self.shard_index,
                    self.worker_id,
                    status.sample_rate,
                    _map_status(status.status),
                    _iso(status.last_audio_at_utc) if status.last_audio_at_utc else None,
                    (status.last_error or "")[:300] or None,
                    _iso(status.started_at_utc),
                ),
            )
            # The planner admits a station as 'starting' and nothing ever
            # advanced it: subscriptions sat at 'starting' forever while their
            # sessions streamed for hours, so every capacity readout lied. The
            # session status is the ground truth, so the transition lives here,
            # in the same transaction as the status row.
            mapped = _map_status(status.status)
            if mapped == "streaming":
                connection.execute(
                    "UPDATE station_subscriptions SET state='active',"
                    " state_reason=NULL, updated_at_utc=?"
                    " WHERE station_id=? AND state IN ('starting','degraded')",
                    (_iso(datetime.now(UTC)), status.station_id),
                )
            elif mapped in {"reconnecting", "failed"}:
                connection.execute(
                    "UPDATE station_subscriptions SET state='degraded',"
                    " state_reason=?, updated_at_utc=?"
                    " WHERE station_id=? AND state IN ('starting','active')",
                    (
                        (status.last_error or mapped)[:300],
                        _iso(datetime.now(UTC)),
                        status.station_id,
                    ),
                )

        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.database.write, write)

    def _usage_percent(self) -> float:
        usage = getattr(self.store, "usage_percent", None)
        return round(usage(), 2) if callable(usage) else 0.0

    def _spool_pressure(self) -> SpoolPressure:
        percent = self._usage_percent()
        settings = self.settings
        if percent >= settings.RADIO_SPOOL_EMERGENCY_PERCENT:
            return "emergency"
        if percent >= settings.RADIO_SPOOL_PAUSE_PERCENT:
            return "pause"
        if percent >= settings.RADIO_SPOOL_WARNING_PERCENT:
            return "warning"
        return "ok"


def _map_status(status: str) -> str:
    """Translate supervisor status to the ``station_sessions`` CHECK values."""
    return {
        "connecting": "connecting",
        "streaming": "streaming",
        "reconnecting": "reconnecting",
        "stopped": "stopped",
        "failed": "failed",
    }.get(status, "connecting")


def main() -> None:
    settings, database = bootstrap("listener")
    try:
        ListenerWorker(settings, database).run()
    finally:
        database.close()


if __name__ == "__main__":
    main()
