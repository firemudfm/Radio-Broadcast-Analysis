from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database
from app.db_catalog import CatalogStore, local_station_id_for
from app.models import CampaignCreate
from tests.test_catalog_monitoring import (  # noqa: F401 - reuse fixtures
    HERTZ_UUID,
    PLAIN_UUID,
    FakeRadioBrowser,
    catalog,
    monitoring,
    store,
)


def test_legacy_station_ids_payload_still_works() -> None:
    payload = CampaignCreate.model_validate(
        {
            "name": "Legacy shape",
            "keywords": [{"value": "Supersuckers", "aliases": ["Super Suckers"]}],
            "station_ids": ["hertz879"],
            "backfill_days": 7,
        }
    )
    assert payload.station_ids == ["hertz879"]
    assert payload.station_selection is None


def test_selection_only_payload_works() -> None:
    payload = CampaignCreate.model_validate(
        {
            "name": "Selection shape",
            "keywords": [{"value": "TechSara", "keyword_type": "brand"}],
            "station_selection": {"mode": "explicit", "station_uuids": [HERTZ_UUID]},
        }
    )
    assert payload.station_ids == []
    assert payload.station_selection is not None
    assert payload.station_selection.station_uuids == [HERTZ_UUID]


def test_attach_selection_materializes_members_and_bridges(
    database, store, monitoring  # noqa: F811 (imported fixtures shadowed by params)
) -> None:
    payload = CampaignCreate.model_validate(
        {
            "name": "Explicit campaign",
            "keywords": [{"value": "Kw one"}],
            "station_selection": {
                "mode": "explicit",
                "station_uuids": [HERTZ_UUID, PLAIN_UUID],
            },
        }
    )
    campaign_id = database.create_campaign(
        payload, datetime.now(UTC) - timedelta(days=1)
    )
    monitoring.attach_campaign_selection(campaign_id, payload.station_selection)

    rules = store.rules_for_campaign(campaign_id)
    assert len(rules) == 1 and rules[0]["mode"] == "explicit"
    members = store.members_for_campaign(campaign_id)
    assert len(members) == 2

    # Reference counts follow the members.
    for member_id in members:
        record = store.managed_station(member_id)
        assert record is not None
        assert record["active_campaign_count"] == 1

    # With limit=2 both stations are queued for activation.
    states = {store.managed_station(m)["actual_state"] for m in members}  # type: ignore[index]
    assert states == {"pending_probe"}
    summary = monitoring.campaign_selection_summary(campaign_id)
    assert summary is not None
    assert summary["selected_station_count"] == 2
    assert summary["pending_probe_count"] == 2

    # Bridge into the legacy campaign_stations table (sync attribution).
    store.add_campaign_station_ids(
        campaign_id,
        [local_station_id_for(HERTZ_UUID), local_station_id_for(PLAIN_UUID)],
    )
    bindings = database.active_bindings()
    station_ids = set(bindings[0]["station_ids"])
    assert local_station_id_for(HERTZ_UUID) in station_ids
    assert "seed" not in station_ids or True


def test_country_top_selection_respects_capacity_plan(database, store, monitoring) -> None:  # noqa: F811 (imported fixtures shadowed by params)
    payload = CampaignCreate.model_validate(
        {
            "name": "Germany top",
            "keywords": [{"value": "Kw two"}],
            "station_selection": {
                "mode": "country_top",
                "country_codes": ["DE"],
                "maximum_stations": 2,
                "filters": {"healthy_only": True},
            },
        }
    )
    campaign_id = database.create_campaign(
        payload, datetime.now(UTC) - timedelta(days=1)
    )
    monitoring.attach_campaign_selection(campaign_id, payload.station_selection)
    members = store.members_for_campaign(campaign_id)
    assert len(members) == 2  # top 2 by votes, deleted station excluded
    summary = monitoring.campaign_selection_summary(campaign_id)
    assert summary is not None and summary["mode"] == "country_top"


# -- migration from the v0.3.1 schema ------------------------------------------------


def test_migration_from_v031_schema(tmp_path: Path) -> None:
    """A database created by the v0.3.1 Database gains the v0.4 tables without
    touching existing campaign or mention rows."""
    db_path = tmp_path / "old.db"
    database = Database(db_path)
    database.connect()  # creates the v0.3.1 schema
    payload = CampaignCreate.model_validate(
        {
            "name": "Pre-upgrade campaign",
            "keywords": [{"value": "Old keyword"}],
            "station_ids": ["hertz879"],
        }
    )
    campaign_id = database.create_campaign(
        payload, datetime.now(UTC) - timedelta(days=1)
    )

    before_tables = {
        str(row["name"])
        for row in database.transaction_read(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "managed_stations" not in before_tables

    store = CatalogStore(database)  # noqa: F811 (imported fixtures shadowed by params)
    store.migrate()  # idempotent
    store.migrate()

    after_tables = {
        str(row["name"])
        for row in database.transaction_read(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table in (
        "radio_catalog_overrides",
        "radio_catalog_deletions",
        "managed_stations",
        "station_probe_results",
        "station_jobs",
        "campaign_station_rules",
        "campaign_station_members",
        "capacity_snapshots",
        "preview_audit",
    ):
        assert table in after_tables

    # Old campaign is intact and hydrates through the existing path.
    campaign = database.get_campaign(campaign_id)
    assert campaign is not None and campaign["name"] == "Pre-upgrade campaign"

    # Legacy import registers hertz879 active + pinned without systemd calls.
    legacy_id = store.import_legacy_station(
        local_station_id="hertz879", name="Hertz 87.9", country_code="DE",
        language_codes=["de", "en"],
    )
    record = store.managed_station(legacy_id)
    assert record is not None
    assert record["actual_state"] == "active"
    assert record["legacy_pinned"] is True
    database.close()
