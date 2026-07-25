from __future__ import annotations

import logging
import signal
import time
from threading import Event

import boto3

from .config import get_settings
from .db import Database
from .services.analysis import MentionAnalysisService
from .services.conversation import ConversationService
from .services.llm import LocalLlmClient
from .services.semantic import SemanticDiscoveryService
from .services.stations import StationService


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("radio-analysis-worker")
    stop = Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    database = Database(settings.RADIO_DATABASE_PATH)
    database.connect()
    s3 = boto3.client("s3", region_name=settings.effective_aws_region)
    conversation = ConversationService(settings, s3)
    llm = LocalLlmClient(settings)
    service = MentionAnalysisService(
        settings,
        database,
        s3,
        conversation,
        llm,
    )
    semantic = SemanticDiscoveryService(
        settings,
        database,
        s3,
        StationService(settings, s3),
        conversation,
        llm,
    )
    logger.info(
        "Shared analysis worker started model=%s batch=%s poll=%ss",
        settings.RADIO_LLM_MODEL,
        settings.RADIO_ANALYSIS_WORKER_BATCH_SIZE,
        settings.RADIO_ANALYSIS_WORKER_POLL_SECONDS,
    )
    try:
        while not stop.is_set():
            try:
                semantic_stats = semantic.scan_once()
                if semantic_stats["groups_processed"] or semantic_stats["errors"]:
                    logger.info("Semantic discovery cycle %s", semantic_stats)
            except Exception:
                logger.exception("Semantic discovery cycle failed")
            mention_ids = database.list_pending_analysis(
                limit=settings.RADIO_ANALYSIS_WORKER_BATCH_SIZE,
                retry_limit=settings.RADIO_ANALYSIS_RETRY_LIMIT,
                settle_seconds=settings.RADIO_ANALYSIS_SETTLE_SECONDS,
            )
            if not mention_ids:
                stop.wait(settings.RADIO_ANALYSIS_WORKER_POLL_SECONDS)
                continue
            for mention_id in mention_ids:
                if stop.is_set():
                    break
                logger.info("Analyzing mention=%s", mention_id)
                result = service.analyze(mention_id, force=False)
                status = ((result or {}).get("analysis") or {}).get("status")
                logger.info("Analysis complete mention=%s status=%s", mention_id, status)
            # Yield CPU and avoid a tight loop when an item repeatedly fails.
            time.sleep(0.2)
    finally:
        database.close()
        logger.info("Shared analysis worker stopped")


if __name__ == "__main__":
    main()
