"""Helpers for creating campaigns through the real API write path.

Deliberately goes through ``Database.create_campaign`` rather than inserting
rows: the planner reads the campaign tables the API writes, so a test that
seeded them directly could pass while the real path was broken.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.db import Database
from app.models import CampaignCreate, KeywordInput

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def create_campaign(
    database: Database,
    *,
    name: str,
    station_ids: list[str],
    keywords: list[tuple[str, str]],
    monitor_from: datetime | None = None,
) -> str:
    """Create one campaign and return its id."""
    payload = CampaignCreate(
        name=name,
        objective="brand_mentions",
        keywords=[
            KeywordInput(value=value, keyword_type=kind)  # type: ignore[arg-type]
            for value, kind in keywords
        ],
        station_ids=station_ids,
        backfill_days=0,
    )
    return database.create_campaign(payload, monitor_from or NOW)


__all__ = ["NOW", "create_campaign"]
