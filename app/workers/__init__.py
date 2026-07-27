"""Shared runtime for the pipeline worker processes.

Every worker is the same shape: build its dependencies, beat a heartbeat, run a
loop until SIGTERM, then shut down cleanly. That shape lives here once so the
individual workers contain only their own logic.

Two rules the base class enforces for all of them:

* **SIGTERM is a request, not a kill.** Docker sends SIGTERM and waits
  ``stop_grace_period`` before SIGKILL. A worker that ignores it loses whatever
  it was holding -- an in-flight segment, an open conversation -- so the signal
  sets an event and the loop finishes its current unit of work.
* **A worker that cannot do its job says so.** The heartbeat row carries a
  status, so ``/readyz`` can distinguish "no listener has ever run" from "the
  listener died four minutes ago" without guessing from queue depth.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any

from ..config import Settings, get_settings
from ..db import Database
from ..observability import configure_logging, log_fields
from ..pipeline.enums import WorkerRole
from ..pipeline.heartbeat import HeartbeatWriter, default_worker_id

logger = logging.getLogger(__name__)


class WorkerShutdown(RuntimeError):
    """Raised internally when a worker is asked to stop mid-operation."""


class BaseWorker:
    """Loop, heartbeat and signal handling for one worker process."""

    role: WorkerRole = "api"

    def __init__(
        self,
        settings: Settings,
        database: Any,
        *,
        worker_id: str | None = None,
        shard_index: int | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.shard_index = (
            settings.RADIO_LISTENER_SHARD_INDEX if shard_index is None else shard_index
        )
        self.worker_id = worker_id or default_worker_id(self.role, self.shard_index)
        self.heartbeat = HeartbeatWriter(
            database,
            worker_id=self.worker_id,
            role=self.role,
            shard_index=self.shard_index,
            shard_count=settings.RADIO_LISTENER_SHARD_COUNT,
            pipeline_mode=settings.RADIO_PIPELINE_MODE,
        )
        self._stop = threading.Event()
        self._last_beat = 0.0

    # -- lifecycle -------------------------------------------------------------

    @property
    def should_stop(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        for received in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(received, self._handle_signal)
            except ValueError:
                # Not the main thread (tests, embedded use). The caller drives
                # request_stop() directly in that case.
                logger.debug("Signal handlers are unavailable off the main thread")
                return

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        logger.info(
            "Shutdown signal received; finishing the current unit of work",
            extra=log_fields(worker_id=self.worker_id, signal=signum),
        )
        self._stop.set()

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep. Returns immediately once stopping."""
        self._stop.wait(timeout=max(0.0, seconds))

    def beat(self, *, status: str = "ok", detail: dict[str, Any] | None = None) -> None:
        """Write a heartbeat, rate-limited to the configured interval."""
        now = time.monotonic()
        if status == "ok" and now - self._last_beat < self.settings.RADIO_HEARTBEAT_INTERVAL_SECONDS:
            return
        self._last_beat = now
        try:
            self.heartbeat.beat(status=status, detail=detail)
        except Exception:  # noqa: BLE001 - a failed heartbeat must not stop work
            logger.warning("Could not write a heartbeat", extra=log_fields(worker_id=self.worker_id))

    # -- the loop --------------------------------------------------------------

    def run(self) -> None:
        """Run until stopped. Subclasses implement :meth:`tick`."""
        self.install_signal_handlers()
        logger.info(
            "Worker starting",
            extra=log_fields(
                worker_id=self.worker_id,
                role=self.role,
                shard_index=self.shard_index,
                pipeline_mode=self.settings.RADIO_PIPELINE_MODE,
            ),
        )
        self.beat(status="starting")
        try:
            self.startup()
            while not self.should_stop:
                try:
                    idle = self.tick()
                except WorkerShutdown:
                    break
                except Exception:  # noqa: BLE001 - one bad cycle must not end the worker
                    logger.exception("Worker cycle failed", extra=log_fields(worker_id=self.worker_id))
                    self.beat(status="degraded")
                    self.sleep(self.error_backoff_seconds)
                    continue
                self.beat()
                if idle:
                    self.sleep(self.idle_sleep_seconds)
        finally:
            logger.info("Worker stopping", extra=log_fields(worker_id=self.worker_id))
            try:
                self.shutdown()
            finally:
                try:
                    self.heartbeat.stop()
                except Exception:  # noqa: BLE001 - shutdown must always complete
                    logger.debug("Could not record the stopped heartbeat")

    # -- overridable -----------------------------------------------------------

    idle_sleep_seconds: float = 1.0
    error_backoff_seconds: float = 5.0

    def startup(self) -> None:
        """Prepare resources. Runs once, before the first tick."""

    def tick(self) -> bool:
        """One unit of work. Return True when there was nothing to do."""
        raise NotImplementedError

    def shutdown(self) -> None:
        """Release resources. Always runs, even after a failure."""


def open_database(settings: Settings) -> Database:
    """Open the shared SQLite database with the required pragmas."""
    database = Database(
        settings.RADIO_DATABASE_PATH,
        mention_window_days=settings.RADIO_MENTION_WINDOW_DAYS,
        mention_audio_pad_seconds=settings.RADIO_MENTION_AUDIO_PAD_SECONDS,
        busy_retries=settings.RADIO_SQLITE_BUSY_RETRIES,
    )
    database.connect()
    return database


def bootstrap(role: str) -> tuple[Settings, Database]:
    """Common start-up for a worker entrypoint.

    Refuses to start a pipeline worker in legacy mode. Starting one anyway
    would silently produce a process that consumes nothing and reports healthy,
    which is worse than not starting (ADR-001).
    """
    settings = get_settings()
    configure_logging(
        level=settings.LOG_LEVEL, log_format=settings.RADIO_LOG_FORMAT
    )
    if not settings.shared_pipeline_enabled:
        raise SystemExit(
            f"Refusing to start the {role} worker: RADIO_PIPELINE_MODE is "
            f"{settings.RADIO_PIPELINE_MODE!r}, not 'shared_sqs'."
        )
    return settings, open_database(settings)


__all__ = ["BaseWorker", "WorkerShutdown", "bootstrap", "open_database"]
