"""Planner worker: desired station state plus the outbox dispatcher.

Two jobs in one process because both are cheap, both are control-plane work,
and both must be singletons:

* reconcile campaigns into one subscription per DISTINCT station and publish
  each station's combined keyword index (:mod:`app.services.subscription_planner`);
* drain the transactional outbox to SQS (:mod:`app.pipeline.outbox`).

The outbox dispatcher lives here rather than in the producing workers so that
exactly one process sends each message. Several dispatchers would still be
*correct* -- leases and consumer inboxes handle it -- but they would multiply
the duplicate-send window for no benefit.

It also sweeps stale job leases, which is what lets work survive a worker that
died holding it.
"""
from __future__ import annotations

import logging

from ..observability import log_fields, safe_extra
from ..pipeline.factory import build_queues
from ..pipeline.heartbeat import HeartbeatReader, StaleJobSweeper
from ..pipeline.outbox import OutboxDispatcher
from ..services.subscription_planner import SubscriptionPlanner
from . import BaseWorker, bootstrap

logger = logging.getLogger(__name__)


class PlannerWorker(BaseWorker):
    role = "planner"

    def __init__(self, settings, database, *, queues=None, **kwargs) -> None:
        super().__init__(settings, database, **kwargs)
        self.planner = SubscriptionPlanner(settings, database)
        self.dispatcher = OutboxDispatcher(
            database,
            queues if queues is not None else build_queues(settings),
            batch_size=settings.RADIO_OUTBOX_BATCH_SIZE,
            max_attempts=settings.RADIO_OUTBOX_MAX_ATTEMPTS,
            lease_seconds=settings.RADIO_OUTBOX_LEASE_SECONDS,
        )
        self.sweeper = StaleJobSweeper(database, max_attempts=settings.RADIO_JOB_MAX_ATTEMPTS)
        self.heartbeats = HeartbeatReader(
            database, stale_after_seconds=settings.RADIO_HEARTBEAT_STALE_SECONDS
        )
        self.idle_sleep_seconds = settings.RADIO_PLANNER_POLL_SECONDS
        self._cycles = 0

    def tick(self) -> bool:
        # Planning first: a new station's index must exist before its segments do.
        plan = self.planner.plan_once()
        dispatched = (
            self.dispatcher.dispatch_once()
            if self.settings.RADIO_OUTBOX_ENABLED
            else {"sent": 0, "claimed": 0}
        )

        self._cycles += 1
        if self._cycles % 12 == 0:
            # Reclaiming is cheap but not free; a stale lease is only
            # interesting on the scale of the lease duration, not every cycle.
            swept = self.sweeper.sweep()
            if any(swept.values()):
                logger.warning("Recovered stale jobs", extra=safe_extra(swept))
            self._prune()

        self.beat(
            status="ok",
            detail={
                **plan.as_dict(),
                "outbox_sent": dispatched.get("sent", 0),
                "outbox_pending": self.dispatcher.stats().get("pending", 0),
            },
        )
        return dispatched.get("claimed", 0) == 0

    def _prune(self) -> None:
        """Bounded retention for the reliability tables."""
        try:
            self.dispatcher.prune(retention_days=self.settings.RADIO_OUTBOX_RETENTION_DAYS)
            self.heartbeats.prune(older_than_days=7)
        except Exception:  # noqa: BLE001 - housekeeping must never stop planning
            logger.warning("Pruning failed", extra=log_fields(worker_id=self.worker_id))


def main() -> None:
    settings, database = bootstrap("planner")
    try:
        PlannerWorker(settings, database).run()
    finally:
        database.close()


if __name__ == "__main__":
    main()
