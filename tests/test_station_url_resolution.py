"""A campaign stores a station id; the listener needs a stream URL.

Nothing bridged the two. ``station_subscriptions.stream_url`` was never written
by anything, so every subscription carried NULL, the listener skipped every
station on every pass, and no audio was ever captured. Both production
campaigns sat at "0 mentions / 7d" while all seven containers reported healthy:

    {"level": "WARNING", "message": "Station has no stream URL; skipping",
     "station_id": "rb-78012206-1aa1-11e9-a80b-52543be04c81"}

...once every five seconds, forever.

The catalogue column it should have come from was empty too: nothing ever
assigned ``managed_stations.stream_url_resolved``, because the Radio Browser
normaliser dropped ``url_resolved`` entirely and the only caller of
``resolve_url`` was the browser preview button.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db_catalog import CatalogStore
from app.services.station_url_resolver import (
    StationUrlResolver,
    station_uuid_from_local_id,
)

STATION_UUID = "78012206-1aa1-11e9-a80b-52543be04c81"
STATION_ID = f"rb-{STATION_UUID}"
STREAM_URL = "https://stream.example.org/live.mp3"
STAMP = "2026-01-01T00:00:00+00:00"


class FakeClient:
    """Stands in for Radio Browser /json/url/<uuid>."""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {}
        self.error = error
        self.calls: list[str] = []

    def resolve_url(self, station_uuid: str) -> dict:
        self.calls.append(station_uuid)
        if self.error is not None:
            raise self.error
        return dict(self.payload)


# =============================================================================
# The id mapping
# =============================================================================


def test_a_radio_browser_id_maps_back_to_its_uuid() -> None:
    assert station_uuid_from_local_id(STATION_ID) == STATION_UUID


@pytest.mark.parametrize("value", ["", "   ", "local-station", "rb-", "78012206"])
def test_an_id_that_is_not_a_radio_browser_id_is_refused(value: str) -> None:
    """Guessing would resolve to another station's stream and attribute its
    mentions to this campaign."""
    assert station_uuid_from_local_id(value) is None


# =============================================================================
# Resolution
# =============================================================================


def test_the_catalogue_is_used_before_the_network(database) -> None:
    store = CatalogStore(database)
    store.migrate()
    store.upsert_managed_station(
        {"station_uuid": STATION_UUID, "name": "Test FM", "stream_url_resolved": STREAM_URL}
    )
    client = FakeClient({"url_resolved": "https://wrong.example/never.mp3"})

    resolver = StationUrlResolver(database, client=client)
    assert resolver.resolve(STATION_ID) == STREAM_URL
    assert client.calls == [], "a stored URL must not cost a Radio Browser call"


def test_an_unresolved_station_is_resolved_once_and_remembered(database) -> None:
    """/json/url counts a click for the station, so asking twice for the same
    answer distorts a free community service's rankings."""
    client = FakeClient({"url_resolved": STREAM_URL, "name": "Test FM"})
    resolver = StationUrlResolver(database, client=client)

    assert resolver.resolve(STATION_ID) == STREAM_URL
    assert client.calls == [STATION_UUID]

    # A second resolver, as if the planner had restarted: the answer is durable.
    again = StationUrlResolver(database, client=FakeClient({}))
    assert again.resolve(STATION_ID) == STREAM_URL


def test_the_resolved_url_is_written_to_the_catalogue(database) -> None:
    resolver = StationUrlResolver(
        database, client=FakeClient({"url_resolved": STREAM_URL, "name": "Test FM"})
    )
    resolver.resolve(STATION_ID)

    store = CatalogStore(database)
    record = store.managed_station_by_uuid(STATION_UUID)
    assert record is not None
    assert record["local_station_id"] == STATION_ID
    # Read through the dedicated accessor: the serialised row deliberately
    # omits stream_url_resolved so it cannot leak into an API response.
    assert "stream_url_resolved" not in record
    assert store.stream_url_for(int(record["id"])) == STREAM_URL


def test_the_resolved_url_is_preferred_over_the_playlist_url(database) -> None:
    """`url` can be a redirecting playlist; `url_resolved` is the stream."""
    resolver = StationUrlResolver(
        database,
        client=FakeClient({"url": "https://example.org/list.pls", "url_resolved": STREAM_URL}),
    )
    assert resolver.resolve(STATION_ID) == STREAM_URL


def test_a_station_with_only_a_plain_url_still_resolves(database) -> None:
    resolver = StationUrlResolver(database, client=FakeClient({"url": STREAM_URL}))
    assert resolver.resolve(STATION_ID) == STREAM_URL


def test_a_radio_browser_failure_is_not_fatal(database) -> None:
    """One unresolvable station must not stop the planner reconciling the rest."""
    resolver = StationUrlResolver(database, client=FakeClient(error=OSError("mirror down")))
    assert resolver.resolve(STATION_ID) is None


def test_an_empty_payload_resolves_to_nothing(database) -> None:
    resolver = StationUrlResolver(database, client=FakeClient({"url": "", "url_resolved": ""}))
    assert resolver.resolve(STATION_ID) is None
    assert CatalogStore(database).managed_station_by_uuid(STATION_UUID) is None


def test_resolution_works_before_the_api_has_ever_started(database) -> None:
    """The planner has its own container and may reach a station first, so it
    cannot assume the API created the catalogue tables."""
    resolver = StationUrlResolver(database, client=FakeClient({"url_resolved": STREAM_URL}))
    assert resolver.resolve(STATION_ID) == STREAM_URL


# =============================================================================
# The planner writes it where the listener looks
# =============================================================================


def make_campaign(database, station_ids: list[str] | None = None) -> None:
    stations = station_ids if station_ids is not None else [STATION_ID]

    def write(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO campaigns(id, name, objective, status, monitor_from_utc,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("c1", "Test", "brand_mentions", "active", STAMP, STAMP, STAMP),
        )
        for station_id in stations:
            connection.execute(
                "INSERT INTO campaign_stations(campaign_id, station_id) VALUES (?,?)",
                ("c1", station_id),
            )
        connection.execute(
            "INSERT INTO campaign_keywords(id, campaign_id, entity_id, value, aliases_json,"
            " match_mode, keyword_type, semantic_matching, semantic_threshold, enabled)"
            " VALUES ('k1','c1','e1','hello','[]','tokens','brand',0,0.74,1)"
        )

    database.write(write)


def planner_for(settings, database, resolver):
    from app.services.subscription_planner import SubscriptionPlanner

    return SubscriptionPlanner(settings, database, url_resolver=resolver)


def subscription(database, station_id: str = STATION_ID) -> dict:
    row = database.read_one(
        "SELECT * FROM station_subscriptions WHERE station_id=?", (station_id,)
    )
    return dict(row) if row is not None else {}


def test_a_new_subscription_gets_its_stream_url(settings, database) -> None:
    """The bug: the planner created the row and left stream_url NULL."""
    make_campaign(database)
    resolver = StationUrlResolver(
        database, client=FakeClient({"url_resolved": STREAM_URL, "name": "Test FM"})
    )
    planner_for(settings, database, resolver).plan_once()

    row = subscription(database)
    assert row["stream_url"] == STREAM_URL
    assert row["station_uuid"] == STATION_UUID
    assert row["display_name"] == "Test FM"


def test_a_subscription_created_before_this_fix_is_backfilled(settings, database) -> None:
    """Reuse never rewrites a subscription, so without a backfill every station
    already on the host would stay skipped forever."""
    make_campaign(database)

    # First cycle with no resolver at all: exactly the rows production has.
    planner_for(settings, database, None).plan_once()
    assert subscription(database)["stream_url"] is None

    resolver = StationUrlResolver(database, client=FakeClient({"url_resolved": STREAM_URL}))
    planner_for(settings, database, resolver).plan_once()
    assert subscription(database)["stream_url"] == STREAM_URL


def test_an_existing_url_is_never_overwritten(settings, database) -> None:
    make_campaign(database)
    client = FakeClient({"url_resolved": "https://replacement.example/live.mp3"})

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO station_subscriptions(station_id, reference_count, state,"
            " shard_index, stream_url, created_at_utc, updated_at_utc)"
            " VALUES (?,1,'active',0,?,?,?)",
            (STATION_ID, STREAM_URL, STAMP, STAMP),
        )

    database.write(seed)
    planner_for(settings, database, StationUrlResolver(database, client=client)).plan_once()

    assert subscription(database)["stream_url"] == STREAM_URL
    assert client.calls == []


def test_a_failed_resolution_backs_off_instead_of_retrying_every_cycle(
    settings, database
) -> None:
    """The planner runs every five seconds. Without a backoff a dead station
    would call Radio Browser 17,000 times a day, and every call counts a click."""
    make_campaign(database)
    client = FakeClient(error=OSError("mirror down"))
    planner = planner_for(settings, database, StationUrlResolver(database, client=client))

    planner.plan_once()
    assert len(client.calls) == 1
    row = subscription(database)
    assert row["stream_url"] is None
    assert row["stream_url_retry_after_utc"] is not None
    assert "could not be resolved" in str(row["last_error"])

    planner.plan_once()
    planner.plan_once()
    assert len(client.calls) == 1, "the backoff was ignored"


def test_the_backoff_expires_so_a_transient_outage_recovers(settings, database) -> None:
    make_campaign(database)
    client = FakeClient({"url_resolved": STREAM_URL})

    def seed(connection: sqlite3.Connection) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        connection.execute(
            "INSERT INTO station_subscriptions(station_id, reference_count, state,"
            " shard_index, stream_url_retry_after_utc, created_at_utc, updated_at_utc)"
            " VALUES (?,1,'active',0,?,?,?)",
            (STATION_ID, past, STAMP, STAMP),
        )

    database.write(seed)
    planner_for(settings, database, StationUrlResolver(database, client=client)).plan_once()

    row = subscription(database)
    assert row["stream_url"] == STREAM_URL
    assert row["stream_url_retry_after_utc"] is None
    assert row["last_error"] is None


def test_resolution_is_bounded_per_cycle(settings, database) -> None:
    """A campaign may reference hundreds of stations; resolving them all in one
    tick would stall the planner behind a burst of HTTP calls."""
    station_ids = [f"rb-{i:08d}-1aa1-11e9-a80b-52543be04c81" for i in range(25)]
    make_campaign(database, station_ids)
    client = FakeClient({"url_resolved": STREAM_URL})
    planner = planner_for(settings, database, StationUrlResolver(database, client=client))

    planner.plan_once()
    budget = settings.RADIO_STATION_URL_RESOLVE_PER_CYCLE
    assert len(client.calls) == budget

    # ...and the rest are picked up by later cycles rather than dropped.
    planner.plan_once()
    assert len(client.calls) == 2 * budget


def test_the_planner_still_works_with_no_resolver(settings, database) -> None:
    """Every existing caller and test constructs the planner without one."""
    make_campaign(database)
    result = planner_for(settings, database, None).plan_once()
    assert result.unique_requested == 1
    assert subscription(database)["stream_url"] is None


# =============================================================================
# The listener stops flooding the log
# =============================================================================


def listener_source() -> str:
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.joinpath(
        "app", "workers", "listener.py"
    ).read_text(encoding="utf-8")


def test_the_listener_warns_once_per_station_not_once_per_cycle() -> None:
    """The production logs were thousands of identical lines, five seconds
    apart, telling nobody anything after the first one."""
    source = listener_source()
    assert "self._unresolved.add(station_id)" in source
    assert "self._unresolved.discard(station_id)" in source


def test_the_listener_reports_recovery() -> None:
    assert "Station now has a stream URL" in listener_source()
