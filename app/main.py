from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import boto3
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.catalog_routes import router as catalog_router
from .api.routes import router
from .config import get_settings
from .db import Database
from .db_catalog import CatalogStore
from .services.analysis import MentionAnalysisService
from .services.audio import AudioService
from .services.campaigns import CampaignService
from .services.catalog import CatalogService
from .services.conversation import ConversationService
from .services.keywords import KeywordConfigService
from .services.llm import LocalLlmClient
from .services.monitoring import MonitoringService
from .services.pipeline_status import PipelineStatusService
from .services.preview import PreviewService
from .services.radio_browser import RadioBrowserClient
from .services.stations import StationService
from .services.sync import IntelligenceSyncService


def _load_bundled_overlay(store: CatalogStore) -> None:
    """Load the packaged curated override/deletion snapshot into SQLite."""
    import json
    from pathlib import Path

    data_dir = Path(__file__).resolve().parent / "data"
    overrides_path = data_dir / "radio_database_overrides.json"
    deletions_path = data_dir / "radio_database_deletions.json"
    if not overrides_path.exists() or not deletions_path.exists():
        logging.getLogger(__name__).warning(
            "Curated radio-database overlay files are missing under %s", data_dir
        )
        return
    overrides = json.loads(overrides_path.read_text(encoding="utf-8"))["overrides"]
    deletions = json.loads(deletions_path.read_text(encoding="utf-8"))["deleted_station_uuids"]
    store.load_overlay(overrides, deletions)
    logging.getLogger(__name__).info(
        "Loaded curated overlay: %d overrides, %d deletions", len(overrides), len(deletions)
    )


def _import_legacy_stations(settings, store: CatalogStore, station_service) -> None:
    """Register already-running pipeline stations (hertz879) as pinned active
    managed stations without touching their systemd units."""
    known = {station["id"]: station for station in station_service.list_stations()}
    for station_id in settings.RADIO_LEGACY_PINNED_STATION_IDS:
        record = known.get(station_id)
        if record is None:
            continue
        store.import_legacy_station(
            local_station_id=station_id,
            name=str(record.get("name") or station_id),
            country_code=record.get("country_code"),
            language_codes=list(record.get("language_codes") or []),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(
        settings.RADIO_DATABASE_PATH,
        mention_window_days=settings.RADIO_MENTION_WINDOW_DAYS,
        mention_audio_pad_seconds=settings.RADIO_MENTION_AUDIO_PAD_SECONDS,
    )
    database.connect()
    s3_client = boto3.client("s3", region_name=settings.effective_aws_region)
    station_service = StationService(settings, s3_client)
    keyword_service = KeywordConfigService(settings, database, s3_client)
    sync_service = IntelligenceSyncService(settings, database, station_service, s3_client)
    campaign_service = CampaignService(database, station_service, keyword_service, sync_service)
    audio_service = AudioService(settings, database, s3_client)
    conversation_service = ConversationService(settings, s3_client)
    llm_client = LocalLlmClient(settings)
    analysis_service = MentionAnalysisService(
        settings,
        database,
        s3_client,
        conversation_service,
        llm_client,
    )

    catalog_store = CatalogStore(database)
    catalog_store.migrate()
    _load_bundled_overlay(catalog_store)
    _import_legacy_stations(settings, catalog_store, station_service)
    radio_browser_client = RadioBrowserClient(
        user_agent=settings.RADIO_BROWSER_USER_AGENT,
        request_timeout_seconds=settings.RADIO_BROWSER_REQUEST_TIMEOUT_SECONDS,
        max_attempts=settings.RADIO_BROWSER_MAX_ATTEMPTS,
        mirror_refresh_seconds=settings.RADIO_BROWSER_MIRROR_REFRESH_SECONDS,
        country_cache_seconds=settings.RADIO_BROWSER_COUNTRY_CACHE_SECONDS,
        search_cache_seconds=settings.RADIO_BROWSER_SEARCH_CACHE_SECONDS,
        station_cache_seconds=settings.RADIO_BROWSER_STATION_CACHE_SECONDS,
    )
    catalog_service = CatalogService(radio_browser_client, catalog_store)
    monitoring_service = MonitoringService(settings, catalog_store, catalog_service)
    preview_service = PreviewService(settings, catalog_store, radio_browser_client)

    app.state.settings = settings
    app.state.database = database
    app.state.s3_client = s3_client
    app.state.station_service = station_service
    app.state.keyword_service = keyword_service
    app.state.sync_service = sync_service
    app.state.campaign_service = campaign_service
    app.state.audio_service = audio_service
    app.state.conversation_service = conversation_service
    app.state.llm_client = llm_client
    app.state.analysis_service = analysis_service
    app.state.catalog_store = catalog_store
    app.state.radio_browser_client = radio_browser_client
    app.state.catalog_service = catalog_service
    app.state.monitoring_service = monitoring_service
    app.state.preview_service = preview_service

    # v0.5 pipeline status. Constructed in BOTH modes so /readyz exists
    # everywhere; it reports legacy readiness (database only) when the shared
    # pipeline is off, and does not require any pipeline worker to be running.
    segment_store = None
    if settings.shared_pipeline_enabled:
        try:
            from .pipeline.factory import build_segment_store

            segment_store = build_segment_store(settings)
        except Exception:  # noqa: BLE001 - status must not block API start-up
            logging.getLogger(__name__).warning(
                "Segment store unavailable; spool usage will report 0"
            )
    app.state.pipeline_status_service = PipelineStatusService(
        settings, database, segment_store=segment_store
    )

    if settings.RADIO_SYNC_ON_STARTUP:
        try:
            await sync_service.sync_once()
        except Exception:
            logging.getLogger(__name__).exception("Initial intelligence sync failed")
    sync_service.start()
    try:
        yield
    finally:
        await sync_service.stop()
        database.close()


settings = get_settings()
app = FastAPI(
    title="FireMud Radio Intelligence API",
    description=(
        "Open pilot API for connected radio stations, campaigns, full radio-chunk "
        "transcripts, highlighted mentions, local multilingual LLM analysis, sentiment, "
        "and private audio playback. No user authentication is enabled."
    ),
    version=settings.RADIO_API_VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.RADIO_API_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Range"],
    expose_headers=["Accept-Ranges", "Content-Length", "Content-Range"],
)
app.include_router(router)
app.include_router(catalog_router)
