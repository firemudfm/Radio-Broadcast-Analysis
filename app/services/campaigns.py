from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import CampaignCreate, CampaignUpdate


class CampaignService:
    def __init__(
        self,
        database: Any,
        station_service: Any,
        keyword_service: Any,
        sync_service: Any,
    ) -> None:
        self._database = database
        self._stations = station_service
        self._keywords = keyword_service
        self._sync = sync_service

    async def list_stations(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._stations.list_stations)

    async def list_campaigns(self) -> list[dict[str, Any]]:
        campaigns = await asyncio.to_thread(self._database.list_campaigns)
        return await self._hydrate(campaigns)

    async def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        campaign = await asyncio.to_thread(self._database.get_campaign, campaign_id)
        if campaign is None:
            return None
        return (await self._hydrate([campaign]))[0]

    async def create_campaign(self, payload: CampaignCreate) -> dict[str, Any]:
        station_map = await asyncio.to_thread(self._stations.station_map)
        missing = [station_id for station_id in payload.station_ids if station_id not in station_map]
        if missing:
            raise ValueError(f"Unknown or disconnected station: {', '.join(missing)}")
        monitor_from = datetime.now(UTC) - timedelta(days=payload.backfill_days)
        campaign_id = await asyncio.to_thread(
            self._database.create_campaign, payload, monitor_from
        )
        await asyncio.to_thread(self._keywords.publish)
        await self._sync.sync_once()
        campaign = await self.get_campaign(campaign_id)
        if campaign is None:
            raise RuntimeError("Campaign was created but could not be loaded")
        return campaign

    async def update_campaign(
        self, campaign_id: str, payload: CampaignUpdate
    ) -> dict[str, Any] | None:
        if payload.station_ids is not None:
            station_map = await asyncio.to_thread(self._stations.station_map)
            missing = [station_id for station_id in payload.station_ids if station_id not in station_map]
            if missing:
                raise ValueError(f"Unknown or disconnected station: {', '.join(missing)}")
        changed = await asyncio.to_thread(self._database.update_campaign, campaign_id, payload)
        if not changed:
            return None
        await asyncio.to_thread(self._keywords.publish)
        await self._sync.sync_once()
        return await self.get_campaign(campaign_id)

    async def delete_campaign(self, campaign_id: str) -> bool:
        deleted = await asyncio.to_thread(self._database.delete_campaign, campaign_id)
        if deleted:
            await asyncio.to_thread(self._keywords.publish)
        return deleted

    async def _hydrate(self, campaigns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        station_map = await asyncio.to_thread(self._stations.station_map)
        output: list[dict[str, Any]] = []
        for campaign in campaigns:
            # Work on a copy. The database representation contains the internal
            # station_ids field, while the public API schema exposes hydrated
            # station objects and forbids unknown fields. Mutating/spreading the
            # original dict previously leaked station_ids into CampaignView and
            # caused every campaign response to fail with HTTP 500.
            campaign_data = dict(campaign)
            station_ids = list(campaign_data.pop("station_ids", []))
            output.append(
                {
                    **campaign_data,
                    "stations": [
                        station_map.get(
                            station_id,
                            {
                                "id": station_id,
                                "name": station_id,
                                "country_code": None,
                                "language_codes": [],
                                "connected": False,
                                "enabled": False,
                            },
                        )
                        for station_id in station_ids
                    ],
                }
            )
        return output
