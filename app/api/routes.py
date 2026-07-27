from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from ..models import (
    AudioTokenResponse,
    CampaignCreate,
    CampaignListResponse,
    CampaignUpdate,
    CampaignView,
    DashboardView,
    HealthView,
    MentionDetailView,
    MentionListResponse,
    PipelineStatusView,
    ReadinessView,
    RuntimeView,
    SentimentSummary,
    StationListResponse,
    SyncView,
)

router = APIRouter()


def _selection_summary(request: Request, campaign: dict) -> dict:
    """Attach the v0.4 station-selection summary to a campaign dict."""
    monitoring = getattr(request.app.state, "monitoring_service", None)
    if monitoring is not None:
        try:
            campaign["selection"] = monitoring.campaign_selection_summary(str(campaign["id"]))
        except Exception:  # noqa: BLE001 - summary is advisory, never fail the campaign call
            campaign["selection"] = None
    return campaign


@router.get("/healthz", response_model=HealthView, tags=["health"])
async def health(request: Request) -> HealthView:
    database_state = "ok" if await asyncio.to_thread(request.app.state.database.ping) else "error"
    s3_state = "ok"
    try:
        await asyncio.to_thread(
            request.app.state.s3_client.list_objects_v2,
            Bucket=request.app.state.settings.RADIO_S3_BUCKET,
            Prefix=request.app.state.settings.RADIO_RESULTS_PREFIX,
            MaxKeys=1,
        )
    except Exception:
        s3_state = "error"
    if not request.app.state.settings.RADIO_LLM_ENABLED:
        llm_state = "disabled"
    else:
        llm_state = "ok" if await asyncio.to_thread(request.app.state.llm_client.health) else "error"
    overall = (
        "ok"
        if database_state == "ok" and s3_state == "ok" and llm_state in {"ok", "disabled"}
        else "degraded"
    )
    settings = request.app.state.settings
    pipeline = None
    status_service = getattr(request.app.state, "pipeline_status_service", None)
    if status_service is not None and settings.shared_pipeline_enabled:
        try:
            pipeline = await asyncio.to_thread(status_service.snapshot)
        except Exception:  # noqa: BLE001 - health must never fail on a sub-report
            pipeline = None
        if pipeline and _pipeline_degraded(pipeline):
            overall = "degraded"
    return HealthView(
        status=overall,
        database=database_state,
        s3=s3_state,
        llm=llm_state,
        sync_enabled=settings.RADIO_SYNC_ENABLED,
        analysis_worker_enabled=settings.RADIO_ANALYSIS_WORKER_ENABLED,
        version=settings.RADIO_API_VERSION,
        pipeline_mode=settings.RADIO_PIPELINE_MODE,
        pipeline=pipeline,
    )


def _pipeline_degraded(snapshot: dict) -> bool:
    components = snapshot.get("components", {})
    if any(state not in {"ok"} for state in components.values() if isinstance(state, str)):
        return True
    return not snapshot.get("queues_configured", True)


@router.get("/readyz", response_model=ReadinessView, tags=["health"])
async def readiness(request: Request, response: Response) -> ReadinessView:
    """Whether this node can serve traffic, as opposed to merely being alive.

    Deliberately lighter than ``/healthz``: SQLite reads and one filesystem
    stat, no S3 and no LLM call. A readiness probe runs constantly, and one
    that does network I/O fails under exactly the load it exists to detect.
    """
    settings = request.app.state.settings
    status_service = getattr(request.app.state, "pipeline_status_service", None)
    if status_service is None:
        alive = await asyncio.to_thread(request.app.state.database.ping)
        return ReadinessView(
            ready=bool(alive),
            pipeline_mode=settings.RADIO_PIPELINE_MODE,
            checks={"database": "ok" if alive else "error"},
        )
    report = await asyncio.to_thread(status_service.readiness)
    if not report["ready"]:
        # 503 so a load balancer or `docker compose` healthcheck can act on it
        # without parsing the body.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessView(**report)


@router.get(
    "/api/v1/monitoring/pipeline",
    response_model=PipelineStatusView,
    tags=["monitoring"],
)
async def pipeline_status(request: Request) -> PipelineStatusView:
    """Shared-pipeline capacity and worker liveness."""
    status_service = getattr(request.app.state, "pipeline_status_service", None)
    if status_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline status is unavailable",
        )
    snapshot = await asyncio.to_thread(status_service.snapshot)
    return PipelineStatusView(**snapshot)


@router.get("/api/v1/brand-signal/runtime", response_model=RuntimeView, tags=["brand-signal"])
async def runtime(request: Request) -> RuntimeView:
    counts = await asyncio.to_thread(request.app.state.database.analysis_counts)
    semantic_counts = await asyncio.to_thread(request.app.state.database.semantic_counts)
    if not request.app.state.settings.RADIO_LLM_ENABLED:
        llm_state = "disabled"
    else:
        llm_state = "ok" if await asyncio.to_thread(request.app.state.llm_client.health) else "error"
    return RuntimeView(
        api_version=request.app.state.settings.RADIO_API_VERSION,
        llm_enabled=request.app.state.settings.RADIO_LLM_ENABLED,
        llm_health=llm_state,
        llm_model=(
            request.app.state.settings.RADIO_LLM_MODEL
            if request.app.state.settings.RADIO_LLM_ENABLED
            else None
        ),
        analysis_worker_enabled=request.app.state.settings.RADIO_ANALYSIS_WORKER_ENABLED,
        analysis_pending=counts["pending"],
        analysis_ready=counts["ready"],
        analysis_errors=counts["error"],
        semantic_discovery_enabled=request.app.state.settings.RADIO_SEMANTIC_DISCOVERY_ENABLED,
        semantic_matched=semantic_counts["matched"],
        semantic_not_matched=semantic_counts["not_matched"],
        semantic_errors=semantic_counts["error"],
    )


@router.get("/api/v1/brand-signal/stations", response_model=StationListResponse, tags=["brand-signal"])
async def list_stations(request: Request) -> StationListResponse:
    stations = await request.app.state.campaign_service.list_stations()
    return StationListResponse(stations=stations)


@router.get("/api/v1/brand-signal/campaigns", response_model=CampaignListResponse, tags=["brand-signal"])
async def list_campaigns(request: Request) -> CampaignListResponse:
    campaigns = await request.app.state.campaign_service.list_campaigns()
    campaigns = [_selection_summary(request, campaign) for campaign in campaigns]
    return CampaignListResponse(campaigns=campaigns)


@router.post(
    "/api/v1/brand-signal/campaigns",
    response_model=CampaignView,
    status_code=status.HTTP_201_CREATED,
    tags=["brand-signal"],
)
async def create_campaign(payload: CampaignCreate, request: Request) -> CampaignView:
    import asyncio

    from ..services.monitoring import MonitoringError
    from ..services.radio_browser import RadioBrowserError

    try:
        campaign = await request.app.state.campaign_service.create_campaign(payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if payload.station_selection is not None:
        monitoring = request.app.state.monitoring_service
        store = request.app.state.catalog_store
        campaign_id = str(campaign["id"])
        try:
            await asyncio.to_thread(
                monitoring.attach_campaign_selection, campaign_id, payload.station_selection
            )
        except MonitoringError as error:
            # The campaign row exists but its selection is invalid: remove it so a
            # retry does not duplicate, then surface the validation problem.
            await request.app.state.campaign_service.delete_campaign(campaign_id)
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        except RadioBrowserError as error:
            await request.app.state.campaign_service.delete_campaign(campaign_id)
            raise HTTPException(
                status_code=502, detail=f"Radio Browser is unreachable: {error}"
            ) from error
        member_ids = await asyncio.to_thread(store.members_for_campaign, campaign_id)
        local_ids: list[str] = []
        for member_id in member_ids:
            record = await asyncio.to_thread(store.managed_station, member_id)
            if record is not None:
                local_ids.append(str(record["local_station_id"]))
        if local_ids:
            await asyncio.to_thread(store.add_campaign_station_ids, campaign_id, local_ids)
            await asyncio.to_thread(store.bump_campaign_revision)
        refreshed = await request.app.state.campaign_service.get_campaign(campaign_id)
        if refreshed is not None:
            campaign = refreshed
    return CampaignView.model_validate(_selection_summary(request, campaign))


@router.get("/api/v1/brand-signal/campaigns/{campaign_id}", response_model=CampaignView, tags=["brand-signal"])
async def get_campaign(campaign_id: str, request: Request) -> CampaignView:
    campaign = await request.app.state.campaign_service.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignView.model_validate(_selection_summary(request, campaign))


@router.patch("/api/v1/brand-signal/campaigns/{campaign_id}", response_model=CampaignView, tags=["brand-signal"])
async def update_campaign(
    campaign_id: str, payload: CampaignUpdate, request: Request
) -> CampaignView:
    try:
        campaign = await request.app.state.campaign_service.update_campaign(campaign_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignView.model_validate(campaign)


@router.post("/api/v1/brand-signal/campaigns/{campaign_id}/start", response_model=CampaignView, tags=["brand-signal"])
async def start_campaign(campaign_id: str, request: Request) -> CampaignView:
    import asyncio

    campaign = await request.app.state.campaign_service.update_campaign(
        campaign_id, CampaignUpdate(status="active")
    )
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    monitoring = getattr(request.app.state, "monitoring_service", None)
    if monitoring is not None:
        await asyncio.to_thread(monitoring.on_campaign_status_change)
    return CampaignView.model_validate(_selection_summary(request, campaign))


@router.post("/api/v1/brand-signal/campaigns/{campaign_id}/stop", response_model=CampaignView, tags=["brand-signal"])
async def stop_campaign(campaign_id: str, request: Request) -> CampaignView:
    import asyncio

    campaign = await request.app.state.campaign_service.update_campaign(
        campaign_id, CampaignUpdate(status="paused")
    )
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    monitoring = getattr(request.app.state, "monitoring_service", None)
    if monitoring is not None:
        await asyncio.to_thread(monitoring.on_campaign_status_change)
    return CampaignView.model_validate(_selection_summary(request, campaign))


@router.delete("/api/v1/brand-signal/campaigns/{campaign_id}", status_code=204, tags=["brand-signal"])
async def delete_campaign(campaign_id: str, request: Request) -> Response:
    import asyncio

    if not await request.app.state.campaign_service.delete_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    monitoring = getattr(request.app.state, "monitoring_service", None)
    if monitoring is not None:
        await asyncio.to_thread(monitoring.on_campaign_status_change)
    return Response(status_code=204)


@router.get("/api/v1/brand-signal/mentions", response_model=MentionListResponse, tags=["brand-signal"])
async def list_mentions(
    request: Request,
    campaign_id: str | None = None,
    station_id: str | None = None,
    sentiment: str | None = Query(default=None, pattern="^(positive|neutral|negative)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MentionListResponse:
    mentions, total = await asyncio.to_thread(
        request.app.state.database.list_mentions,
        campaign_id=campaign_id,
        station_id=station_id,
        sentiment=sentiment,
        limit=limit,
        offset=offset,
    )
    return MentionListResponse(mentions=mentions, total=total)


@router.get(
    "/api/v1/brand-signal/mentions/{mention_id}/detail",
    response_model=MentionDetailView,
    tags=["brand-signal"],
)
async def mention_detail(
    mention_id: str,
    request: Request,
    refresh_analysis: bool = Query(default=False),
) -> MentionDetailView:
    result = await asyncio.to_thread(
        request.app.state.analysis_service.detail,
        mention_id,
        refresh=refresh_analysis,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Mention not found")
    return MentionDetailView.model_validate(result)


@router.post(
    "/api/v1/brand-signal/mentions/{mention_id}/analysis",
    response_model=MentionDetailView,
    tags=["brand-signal"],
)
async def analyze_mention(
    mention_id: str,
    request: Request,
    force: bool = Query(default=True),
) -> MentionDetailView:
    result = await asyncio.to_thread(
        request.app.state.analysis_service.analyze,
        mention_id,
        force=force,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Mention not found")
    return MentionDetailView.model_validate(result)


@router.get("/api/v1/brand-signal/dashboard", response_model=DashboardView, tags=["brand-signal"])
async def dashboard(
    request: Request,
    mention_limit: int = Query(default=100, ge=1, le=200),
) -> DashboardView:
    campaigns = await request.app.state.campaign_service.list_campaigns()
    mentions, total = await asyncio.to_thread(
        request.app.state.database.list_mentions, limit=mention_limit, offset=0
    )
    sentiment = await asyncio.to_thread(request.app.state.database.sentiment_summary)
    return DashboardView(
        campaigns=campaigns,
        mentions=mentions,
        sentiment=SentimentSummary.model_validate(sentiment),
        total_mentions=total,
        generated_at_utc=datetime.now(UTC),
        mention_window_days=request.app.state.settings.RADIO_MENTION_WINDOW_DAYS,
    )


@router.post(
    "/api/v1/brand-signal/mentions/{mention_id}/audio-token",
    response_model=AudioTokenResponse,
    tags=["brand-signal"],
)
async def create_audio_token(mention_id: str, request: Request) -> AudioTokenResponse:
    result = await asyncio.to_thread(
        request.app.state.audio_service.create_token, mention_id, request
    )
    return AudioTokenResponse.model_validate(result)


@router.get("/api/v1/brand-signal/audio/{token}", name="stream_audio", tags=["brand-signal"])
async def stream_audio(
    token: str,
    request: Request,
    range_header: str | None = Header(default=None, alias="Range"),
):
    return await asyncio.to_thread(request.app.state.audio_service.stream, token, range_header)


@router.post("/api/v1/brand-signal/sync", response_model=SyncView, tags=["brand-signal"])
async def run_sync(request: Request) -> SyncView:
    return SyncView.model_validate(await request.app.state.sync_service.sync_once())
