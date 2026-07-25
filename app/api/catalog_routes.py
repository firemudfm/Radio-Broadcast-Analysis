"""v0.4 radio catalogue and monitoring routes.

Public responses never include stream URLs. The browser deals in station
UUIDs and short-lived FastAPI preview tokens only.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..models_catalog import (
    ActivateStationResponse,
    CapacityView,
    CatalogStation,
    CatalogStationPage,
    CountryListResponse,
    LanguageListResponse,
    ManagedStationListResponse,
    ManagedStationView,
    NamedOptionListResponse,
    PreviewTokenResponse,
    StationEstimateRequest,
    StationEstimateView,
    StationJobView,
    StationProbeResultView,
    StopStationResponse,
)
from ..services.monitoring import MonitoringError
from ..services.preview import PreviewError
from ..services.radio_browser import RadioBrowserError

router = APIRouter()


def _catalog(request: Request):
    return request.app.state.catalog_service


def _monitoring(request: Request):
    return request.app.state.monitoring_service


def _preview(request: Request):
    return request.app.state.preview_service


def _store(request: Request):
    return request.app.state.catalog_store


def _map_errors(error: Exception) -> HTTPException:
    if isinstance(error, MonitoringError):
        return HTTPException(status_code=error.status_code, detail=error.detail)
    if isinstance(error, PreviewError):
        return HTTPException(status_code=error.status_code, detail=error.detail)
    if isinstance(error, RadioBrowserError):
        return HTTPException(status_code=502, detail=f"Radio Browser is unreachable: {error}")
    raise error


# -- catalogue -----------------------------------------------------------------


@router.get("/api/v1/radio-catalog/countries", response_model=CountryListResponse, tags=["radio-catalog"])
async def list_countries(request: Request) -> CountryListResponse:
    try:
        items = await asyncio.to_thread(_catalog(request).countries)
    except RadioBrowserError as error:
        raise _map_errors(error) from error
    return CountryListResponse(items=items)


@router.get("/api/v1/radio-catalog/languages", response_model=LanguageListResponse, tags=["radio-catalog"])
async def list_languages(request: Request) -> LanguageListResponse:
    try:
        items = await asyncio.to_thread(_catalog(request).languages)
    except RadioBrowserError as error:
        raise _map_errors(error) from error
    return LanguageListResponse(items=items)


@router.get("/api/v1/radio-catalog/tags", response_model=NamedOptionListResponse, tags=["radio-catalog"])
async def list_tags(request: Request) -> NamedOptionListResponse:
    try:
        items = await asyncio.to_thread(_catalog(request).tags)
    except RadioBrowserError as error:
        raise _map_errors(error) from error
    return NamedOptionListResponse(items=items)


@router.get("/api/v1/radio-catalog/codecs", response_model=NamedOptionListResponse, tags=["radio-catalog"])
async def list_codecs(request: Request) -> NamedOptionListResponse:
    try:
        items = await asyncio.to_thread(_catalog(request).codecs)
    except RadioBrowserError as error:
        raise _map_errors(error) from error
    return NamedOptionListResponse(items=items)


@router.get("/api/v1/radio-catalog/stations", response_model=CatalogStationPage, tags=["radio-catalog"])
async def search_stations(
    request: Request,
    country_code: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    query: Annotated[str | None, Query(max_length=200)] = None,
    state: Annotated[str | None, Query(max_length=120)] = None,
    language: Annotated[str | None, Query(max_length=60)] = None,
    tag: Annotated[str | None, Query(max_length=60)] = None,
    tag_list: Annotated[str | None, Query(max_length=400)] = None,
    codec: Annotated[str | None, Query(max_length=30)] = None,
    bitrate_min: Annotated[int | None, Query(ge=0, le=2048)] = None,
    bitrate_max: Annotated[int | None, Query(ge=0, le=2048)] = None,
    https_only: bool = False,
    healthy_only: bool = True,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    order: Annotated[str, Query(max_length=30)] = "votes",
    reverse: bool = True,
) -> CatalogStationPage:
    tags = [item.strip() for item in (tag_list or "").split(",") if item.strip()]
    try:
        page = await asyncio.to_thread(
            lambda: _catalog(request).search_stations(
                country_code=country_code.upper() if country_code else None,
                query=query,
                state=state,
                language=language,
                tag=tag,
                tag_list=tags or None,
                codec=codec,
                bitrate_min=bitrate_min,
                bitrate_max=bitrate_max,
                https_only=https_only,
                healthy_only=healthy_only,
                offset=offset,
                limit=limit,
                order=order,
                reverse=reverse,
            )
        )
    except RadioBrowserError as error:
        raise _map_errors(error) from error
    return CatalogStationPage.model_validate(page)


@router.get(
    "/api/v1/radio-catalog/stations/{station_uuid}",
    response_model=CatalogStation,
    tags=["radio-catalog"],
)
async def station_detail(station_uuid: str, request: Request) -> CatalogStation:
    try:
        station = await asyncio.to_thread(_catalog(request).station_by_uuid, station_uuid)
    except RadioBrowserError as error:
        raise _map_errors(error) from error
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found or removed")
    return CatalogStation.model_validate(station)


# -- probe and preview ------------------------------------------------------------


@router.post(
    "/api/v1/radio-catalog/stations/{station_uuid}/probe",
    response_model=ActivateStationResponse,
    tags=["radio-catalog"],
)
async def request_probe(station_uuid: str, request: Request) -> ActivateStationResponse:
    try:
        outcome = await asyncio.to_thread(_monitoring(request).request_probe, station_uuid)
    except (MonitoringError, RadioBrowserError) as error:
        raise _map_errors(error) from error
    return ActivateStationResponse(
        managed_station_id=outcome["managed_station_id"],
        station_uuid=station_uuid.lower(),
        actual_state=outcome["state"],
        job_id=outcome["job_id"],
        detail="Probe queued for the station reconciler"
        if outcome["job_id"]
        else "Station is already active",
    )


@router.post(
    "/api/v1/radio-catalog/stations/{station_uuid}/preview-token",
    response_model=PreviewTokenResponse,
    tags=["radio-catalog"],
)
async def create_preview_token(station_uuid: str, request: Request) -> PreviewTokenResponse:
    catalog = _catalog(request)
    try:
        deleted = await asyncio.to_thread(catalog.is_deleted, station_uuid)
        token = await asyncio.to_thread(
            lambda: _preview(request).create_token(station_uuid, deleted=deleted)
        )
    except PreviewError as error:
        raise _map_errors(error) from error
    return PreviewTokenResponse(
        url=str(request.url_for("stream_station_preview", token=token["token"])),
        expires_at_utc=token["expires_at_utc"],
        max_seconds=token["max_seconds"],
    )


@router.get(
    "/api/v1/radio-catalog/preview/{token}",
    name="stream_station_preview",
    tags=["radio-catalog"],
)
async def stream_station_preview(token: str, request: Request) -> StreamingResponse:
    try:
        generator, content_type = await asyncio.to_thread(
            _preview(request).stream_preview, token
        )
    except (PreviewError, RadioBrowserError) as error:
        raise _map_errors(error) from error
    return StreamingResponse(
        generator,
        media_type=content_type,
        headers={"Cache-Control": "no-store", "X-Preview-Max-Seconds": str(
            request.app.state.settings.RADIO_PREVIEW_MAX_SECONDS
        )},
    )


# -- monitoring ---------------------------------------------------------------------


@router.get("/api/v1/monitoring/capacity", response_model=CapacityView, tags=["monitoring"])
async def capacity(request: Request) -> CapacityView:
    snapshot = await asyncio.to_thread(_monitoring(request).capacity)
    return CapacityView.model_validate(snapshot)


@router.get(
    "/api/v1/monitoring/stations",
    response_model=ManagedStationListResponse,
    tags=["monitoring"],
)
async def managed_stations(request: Request) -> ManagedStationListResponse:
    records = await asyncio.to_thread(_store(request).list_managed_stations)
    return ManagedStationListResponse(
        items=[ManagedStationView.model_validate(record) for record in records]
    )


@router.post(
    "/api/v1/monitoring/stations/estimate",
    response_model=StationEstimateView,
    tags=["monitoring"],
)
async def estimate(payload: StationEstimateRequest, request: Request) -> StationEstimateView:
    try:
        outcome = await asyncio.to_thread(
            _monitoring(request).estimate_selection, payload.station_selection
        )
    except (MonitoringError, RadioBrowserError) as error:
        raise _map_errors(error) from error
    return StationEstimateView.model_validate(outcome)


@router.post(
    "/api/v1/monitoring/stations/{station_uuid}/activate",
    response_model=ActivateStationResponse,
    tags=["monitoring"],
)
async def activate_station(station_uuid: str, request: Request) -> ActivateStationResponse:
    try:
        outcome = await asyncio.to_thread(_monitoring(request).request_activation, station_uuid)
    except (MonitoringError, RadioBrowserError) as error:
        raise _map_errors(error) from error
    return ActivateStationResponse.model_validate(outcome)


@router.post(
    "/api/v1/monitoring/stations/{managed_station_id}/stop",
    response_model=StopStationResponse,
    tags=["monitoring"],
)
async def stop_station(managed_station_id: int, request: Request) -> StopStationResponse:
    try:
        outcome = await asyncio.to_thread(_monitoring(request).request_stop, managed_station_id)
    except MonitoringError as error:
        raise _map_errors(error) from error
    return StopStationResponse.model_validate(outcome)


@router.get(
    "/api/v1/monitoring/stations/{managed_station_id}/status",
    response_model=ManagedStationView,
    tags=["monitoring"],
)
async def station_status(managed_station_id: int, request: Request) -> ManagedStationView:
    record = await asyncio.to_thread(_store(request).managed_station, managed_station_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown managed station")
    return ManagedStationView.model_validate(record)


@router.get(
    "/api/v1/monitoring/stations/{managed_station_id}/probe-result",
    response_model=StationProbeResultView,
    tags=["monitoring"],
)
async def probe_result(managed_station_id: int, request: Request) -> StationProbeResultView:
    record = await asyncio.to_thread(_store(request).managed_station, managed_station_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown managed station")
    result = await asyncio.to_thread(_store(request).latest_probe_result, managed_station_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No probe result recorded yet")
    return StationProbeResultView(
        managed_station_id=managed_station_id,
        station_uuid=record["station_uuid"],
        status=result["status"],
        codec=result["codec"],
        sample_rate=result["sample_rate"],
        channels=result["channels"],
        duration_seconds=result["duration_seconds"],
        error=result["error"],
        checked_at_utc=result["checked_at_utc"],
    )


@router.get(
    "/api/v1/monitoring/jobs",
    response_model=list[StationJobView],
    tags=["monitoring"],
)
async def list_jobs(request: Request) -> list[StationJobView]:
    jobs = await asyncio.to_thread(_store(request).list_jobs)
    return [StationJobView.model_validate(job) for job in jobs]
