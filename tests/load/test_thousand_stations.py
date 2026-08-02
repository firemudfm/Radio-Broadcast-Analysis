"""Synthetic load at the 1,000-station scale (ADR-008).

What this test is and is not
----------------------------
It proves the **control plane** holds at the scale in the capacity
requirement: 1,000 station records, many campaigns sharing them, and tens of
thousands of keywords. Planning, index building, matching and the capacity
counters all have to stay correct and bounded there.

It does **not** prove that one host can transcribe 1,000 live streams, and
nothing here should ever be quoted as if it did. Actual simultaneous capture
capacity is a separate, measured number -- see ADR-008 and
docs/QUALITY_EVALUATION.md. The distinction is the whole point of keeping
``unique_active_station_count`` separate from
``campaign_station_reference_count``.

The timing assertions are deliberately loose. They are regression guards
against an accidental O(n^2) -- the kind of change that looks fine at ten
stations and takes minutes at a thousand -- not performance targets.
"""
from __future__ import annotations

import json
import time

import pytest

from app.config import Settings
from app.db import Database
from app.services.keyword_index import build_index
from app.services.keyword_matcher import KeywordMatcher, clear_compiled_cache
from app.services.pipeline_status import PipelineStatusService
from app.services.subscription_planner import SubscriptionPlanner
from tests.fixtures.campaigns import create_campaign

pytestmark = pytest.mark.load

STATION_COUNT = 1_000
CAMPAIGN_COUNT = 40
STATIONS_PER_CAMPAIGN = 50
KEYWORDS_PER_CAMPAIGN = 25


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="load-test",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_SPOOL_PATH=tmp_path / "spool",
        RADIO_QUEUE_BACKEND="memory",
        # The whole point: far fewer active stations than requested ones.
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8,
        RADIO_LISTENER_MAX_SESSIONS=8,
        RADIO_MAX_STATIONS_PER_CAMPAIGN=100,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def loaded(database: Database, settings: Settings):
    """40 campaigns x 50 stations x 25 keywords over 1,000 distinct stations."""
    stations = [f"rb-station-{index:04d}" for index in range(STATION_COUNT)]
    started = time.perf_counter()
    for campaign in range(CAMPAIGN_COUNT):
        offset = (campaign * STATIONS_PER_CAMPAIGN) % STATION_COUNT
        selected = [
            stations[(offset + step) % STATION_COUNT]
            for step in range(STATIONS_PER_CAMPAIGN)
        ]
        create_campaign(
            database,
            name=f"Load Campaign {campaign:03d}",
            station_ids=selected,
            keywords=[
                (f"Brand{campaign:03d}x{keyword:03d}", "brand")
                for keyword in range(KEYWORDS_PER_CAMPAIGN)
            ],
        )
    elapsed = time.perf_counter() - started
    print(f"\n  seeded {CAMPAIGN_COUNT} campaigns in {elapsed:.1f}s")
    return stations


# --- planning -----------------------------------------------------------------


def test_planning_at_scale_stays_bounded_and_correct(
    settings: Settings, database: Database, loaded
) -> None:
    planner = SubscriptionPlanner(settings, database)

    started = time.perf_counter()
    plan = planner.plan_once()
    elapsed = time.perf_counter() - started
    print(f"  planner cycle: {elapsed:.1f}s for {plan.unique_requested} unique stations")

    expected_unique = min(STATION_COUNT, CAMPAIGN_COUNT * STATIONS_PER_CAMPAIGN)
    assert plan.unique_requested == expected_unique

    # Capacity is expressed in ACTIVE stations, and the overflow is visible.
    assert plan.unique_active == settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS
    assert plan.pending_capacity == expected_unique - plan.unique_active
    assert plan.pending_capacity > 0, "the point of the test is that most stations wait"

    assert elapsed < 120.0, "planning is superlinear; check for an accidental O(n^2)"


def test_the_counters_do_not_conflate_campaigns_with_capacity(
    settings: Settings, database: Database, loaded
) -> None:
    """The claim ADR-008 exists to prevent: reference count is not capacity."""
    SubscriptionPlanner(settings, database).plan_once()
    status = PipelineStatusService(settings, database)
    capacity = status.capacity()

    assert capacity["campaign_station_reference_count"] == CAMPAIGN_COUNT * STATIONS_PER_CAMPAIGN
    assert capacity["unique_requested_station_count"] == STATION_COUNT
    assert capacity["unique_active_station_count"] == settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS
    assert (
        capacity["unique_active_station_count"]
        < capacity["unique_requested_station_count"]
        <= capacity["campaign_station_reference_count"]
    )
    print(
        f"  references={capacity['campaign_station_reference_count']} "
        f"requested={capacity['unique_requested_station_count']} "
        f"active={capacity['unique_active_station_count']} "
        f"pending={capacity['pending_capacity_station_count']}"
    )


def test_one_subscription_per_distinct_station_however_many_campaigns(
    settings: Settings, database: Database, loaded
) -> None:
    SubscriptionPlanner(settings, database).plan_once()

    rows = database.read_all("SELECT station_id, reference_count FROM station_subscriptions")
    station_ids = [str(row["station_id"]) for row in rows]
    assert len(station_ids) == len(set(station_ids)), "a station must never be subscribed twice"

    shared = [row for row in rows if int(row["reference_count"]) > 1]
    assert shared, "the seed data overlaps campaigns, so sharing must be visible"
    print(f"  {len(rows)} subscriptions, {len(shared)} shared by 2+ campaigns")


def test_one_keyword_index_per_station(settings: Settings, database: Database, loaded) -> None:
    SubscriptionPlanner(settings, database).plan_once()
    rows = database.read_all(
        "SELECT station_id, count(*) AS versions FROM station_keyword_index_versions"
        " GROUP BY station_id HAVING versions > 1"
    )
    assert rows == [], "a first planning pass must publish exactly one version per station"


def test_replanning_is_idempotent_and_does_not_churn_indexes(
    settings: Settings, database: Database, loaded
) -> None:
    """A no-op cycle must not republish, or every listener reloads for nothing."""
    planner = SubscriptionPlanner(settings, database)
    planner.plan_once()
    before = database.read_all("SELECT count(*) AS n FROM station_keyword_index_versions")[0]["n"]

    started = time.perf_counter()
    second = planner.plan_once()
    elapsed = time.perf_counter() - started

    after = database.read_all("SELECT count(*) AS n FROM station_keyword_index_versions")[0]["n"]
    assert after == before, "content did not change, so no new version may be published"
    assert second.index_versions_published == 0
    print(f"  idempotent replan: {elapsed:.1f}s, 0 new index versions")


# --- matching -----------------------------------------------------------------


def test_matching_scales_to_tens_of_thousands_of_keywords() -> None:
    """One scan over one transcript, whatever the index size."""
    clear_compiled_cache()
    total_keywords = 20_000
    bindings = [
        {
            "keyword_id": f"kw-{index:06d}",
            "campaign_id": f"campaign-{index % 100:03d}",
            "entity_id": f"brand{index:06d}",
            "canonical_value": f"Brand{index:06d}",
            "keyword_type": "brand",
            "match_mode": "tokens",
            "aliases": [],
            "languages": [],
            "content_policy": {},
        }
        for index in range(total_keywords)
    ]
    bindings.append(
        {
            "keyword_id": "kw-target",
            "campaign_id": "campaign-target",
            "entity_id": "nvidia",
            "canonical_value": "NVIDIA",
            "keyword_type": "brand",
            "match_mode": "tokens",
            "aliases": [{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}],
            "languages": ["en", "hi"],
            "content_policy": {},
        }
    )

    started = time.perf_counter()
    index = build_index("rb-load", bindings)
    build_seconds = time.perf_counter() - started

    started = time.perf_counter()
    matcher = KeywordMatcher(index)
    compile_seconds = time.perf_counter() - started

    transcript = (
        "Welcome back to the show. Today we look at the new NVIDIA hardware "
        "and what it means for buyers. " * 20
    )
    started = time.perf_counter()
    for _ in range(50):
        report = matcher.match(transcript)
    scan_seconds = (time.perf_counter() - started) / 50

    print(
        f"\n  {total_keywords + 1} keywords -> {matcher.term_count} terms"
        f"\n  build {build_seconds:.1f}s, compile {compile_seconds:.1f}s,"
        f" scan {scan_seconds * 1000:.1f}ms per transcript"
    )

    assert report.keyword_ids == ("kw-target",), "no false positives at scale"
    # A 20-second segment arrives every 20 seconds per station. Even at 8
    # stations that is one scan every 2.5s, so tens of milliseconds is ample --
    # this bound only catches a regression to per-term scanning.
    assert scan_seconds < 0.5, "matching became linear in the number of keywords"
    assert compile_seconds < 60.0


def test_matching_is_compiled_once_per_index_version() -> None:
    """Recompiling per segment would dominate the cost of transcribing one."""
    clear_compiled_cache()
    bindings = [
        {
            "keyword_id": f"kw-{index}",
            "campaign_id": "campaign-a",
            "entity_id": f"b{index}",
            "canonical_value": f"Brand{index:05d}",
            "keyword_type": "brand",
            "match_mode": "tokens",
            "aliases": [],
            "languages": [],
            "content_policy": {},
        }
        for index in range(5_000)
    ]
    index = build_index("rb-cache", bindings)

    started = time.perf_counter()
    KeywordMatcher(index)
    cold = time.perf_counter() - started

    started = time.perf_counter()
    for _ in range(20):
        KeywordMatcher(index)
    warm = (time.perf_counter() - started) / 20

    print(f"  compile cold {cold * 1000:.0f}ms, warm {warm * 1000:.2f}ms")
    assert warm < cold / 10, "the compiled automaton is not being reused"


# --- memory -------------------------------------------------------------------


def test_ring_buffer_memory_is_a_predictable_constant() -> None:
    """Per-station memory must be knowable before the process starts."""
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_RING_BUFFER_SECONDS=60,
        RADIO_SAMPLE_RATE=16_000,
    )
    per_station = settings.ring_buffer_bytes_per_station
    assert per_station == 60 * 16_000 * 2

    for station_count in (8, 32, 128):
        total_mib = per_station * station_count / 1_048_576
        print(f"  {station_count:4d} stations -> {total_mib:7.1f} MiB of ring buffers")

    # The configured default must fit comfortably inside the listener's
    # 1024 MiB container limit from compose.prod.yaml.
    assert per_station * settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS < 256 * 1_048_576


# --- production capacity truth ------------------------------------------------
#
# The tests above run with eight active slots so sharing behaviour is visible.
# Production runs with ONE. These pin what that actually means, because "we
# support 1,000 stations" is only ever true of the control plane.


@pytest.fixture
def production_settings(tmp_path) -> Settings:
    """The real production capacity: request a thousand, decode one."""
    return Settings(
        RADIO_S3_BUCKET="load-test",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_SPOOL_PATH=tmp_path / "spool",
        RADIO_QUEUE_BACKEND="memory",
        RADIO_MAX_REQUESTED_UNIQUE_STATIONS=1000,
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=1,
        RADIO_LISTENER_MAX_SESSIONS=1,
        RADIO_MAX_STATIONS_PER_CAMPAIGN=100,
    )


@pytest.fixture
def production_database(production_settings: Settings) -> Database:
    db = Database(production_settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


def test_one_thousand_requested_stations_park_nine_hundred_and_ninety_nine(
    production_settings: Settings, production_database: Database
) -> None:
    """requested=1000, active=1, pending_capacity=999.

    This is the shape of the truth. A thousand stations are accepted, recorded
    and indexed; exactly one is decoded; the other 999 wait for a slot. None is
    silently dropped, and none is silently started.
    """
    # Ten campaigns of a hundred stations each. One campaign of a thousand is
    # refused by RADIO_MAX_STATIONS_PER_CAMPAIGN, which is a deliberate product
    # limit and not something a load test should widen.
    for campaign in range(10):
        create_campaign(
            production_database,
            name=f"Everything {campaign:02d}",
            station_ids=[
                f"rb-prod-{campaign * 100 + index:04d}" for index in range(100)
            ],
            keywords=[("NVIDIA", "brand")],
        )

    planner = SubscriptionPlanner(production_settings, production_database)
    result = planner.plan_once()

    assert result.unique_requested == 1000, "every requested station is recorded"
    assert result.unique_active == 1, "one station is decoded on this host"
    assert result.pending_capacity == 999, "the rest wait; none is dropped"

    snapshot = planner.capacity_snapshot()
    assert snapshot["unique_requested_station_count"] == 1000
    assert snapshot["unique_active_station_count"] == 1
    assert snapshot["pending_capacity_station_count"] == 999
    assert snapshot["active_unique_station_limit"] == 1

    # Nothing lost: the two states account for every requested station.
    assert (
        snapshot["unique_active_station_count"]
        + snapshot["pending_capacity_station_count"]
        == snapshot["unique_requested_station_count"]
    )


def test_a_parked_station_says_why(
    production_settings: Settings, production_database: Database
) -> None:
    """An operator looking at 999 parked stations must not have to guess."""
    create_campaign(
        production_database,
        name="Everything",
        station_ids=[f"rb-prod-{index:04d}" for index in range(50)],
        keywords=[("NVIDIA", "brand")],
    )
    SubscriptionPlanner(production_settings, production_database).plan_once()

    parked = production_database.read_all(
        "SELECT state_reason FROM station_subscriptions WHERE state='pending_capacity'"
    )
    assert len(parked) == 49
    assert all("limit reached" in str(row["state_reason"]) for row in parked)


def test_one_hundred_campaigns_on_one_station_open_one_subscription(
    production_settings: Settings, production_database: Database
) -> None:
    """The core economic claim. A hundred customers watching the same station
    is one decode, not a hundred -- and one decode is all this host has."""
    for index in range(100):
        create_campaign(
            production_database,
            name=f"Campaign {index:03d}",
            station_ids=["rb-shared-0001"],
            keywords=[(f"Brand{index:03d}", "brand")],
        )

    planner = SubscriptionPlanner(production_settings, production_database)
    result = planner.plan_once()
    snapshot = planner.capacity_snapshot()

    assert snapshot["campaign_station_reference_count"] == 100, "100 campaign links"
    assert result.unique_requested == 1, "one distinct station"
    assert result.unique_active == 1, "one subscription, one decode"
    assert result.pending_capacity == 0

    rows = production_database.read_all(
        "SELECT station_id, reference_count FROM station_subscriptions"
    )
    assert len(rows) == 1, "one row, not one hundred"
    assert rows[0]["reference_count"] == 100


def test_removing_one_of_a_hundred_campaigns_keeps_the_station(
    production_settings: Settings, production_database: Database
) -> None:
    """Reference counting, not last-writer-wins. Ninety-nine campaigns still
    need this station."""
    for index in range(100):
        create_campaign(
            production_database,
            name=f"Campaign {index:03d}",
            station_ids=["rb-shared-0001"],
            keywords=[(f"Brand{index:03d}", "brand")],
        )
    planner = SubscriptionPlanner(production_settings, production_database)
    planner.plan_once()

    production_database.write(
        lambda connection: connection.execute(
            "DELETE FROM campaigns WHERE name='Campaign 000'"
        )
    )
    result = planner.plan_once()

    assert result.unique_active == 1, "the station is still needed"
    rows = production_database.read_all(
        "SELECT reference_count FROM station_subscriptions WHERE state != 'stopped'"
    )
    assert rows[0]["reference_count"] == 99


def test_ten_thousand_keyword_bindings_build_one_index_per_station(
    production_settings: Settings, production_database: Database
) -> None:
    """200 campaigns x 50 keywords, all on one station: one combined index.

    Fifty per campaign is the product limit; a load test widens the campaign
    count rather than the limit.
    """
    for campaign in range(200):
        create_campaign(
            production_database,
            name=f"Keyword Campaign {campaign:03d}",
            station_ids=["rb-shared-0001"],
            keywords=[
                (f"Brand{campaign:03d}x{keyword:02d}", "brand")
                for keyword in range(50)
            ],
        )

    planner = SubscriptionPlanner(production_settings, production_database)
    planner.plan_once()

    indexes = production_database.read_all(
        "SELECT station_id, keyword_count, campaign_count, payload_json "
        "FROM station_keyword_index_versions ORDER BY version DESC"
    )
    assert len(indexes) == 1, "one combined index per station, not per campaign"
    assert indexes[0]["campaign_count"] == 200
    assert indexes[0]["keyword_count"] == 10_000, "every binding is in the one index"

    payload = json.loads(indexes[0]["payload_json"])
    # One term per distinct surface form. Every keyword here is unique, so the
    # term count matches the binding count.
    assert len(payload["terms"]) == 10_000


def test_duplicate_terms_collapse_but_keep_every_owner(
    production_settings: Settings, production_database: Database
) -> None:
    """Fifty campaigns tracking the same word is one scan term and fifty
    owners. Scanning it fifty times would be fifty times the CPU for one
    answer; losing the owners would break attribution."""
    for index in range(50):
        create_campaign(
            production_database,
            name=f"Duplicate {index:02d}",
            station_ids=["rb-shared-0001"],
            keywords=[("NVIDIA", "brand")],
        )

    SubscriptionPlanner(production_settings, production_database).plan_once()
    index_row = production_database.read_all(
        "SELECT payload_json FROM station_keyword_index_versions"
    )[0]
    terms = json.loads(index_row["payload_json"])["terms"]

    scan_terms = {str(term["normalized"]).casefold() for term in terms}
    assert scan_terms == {"nvidia"}, "one term to scan for, not fifty"

    # One term, fifty owning keywords. Losing the owners would break attribution;
    # scanning fifty times would be fifty times the CPU for one answer.
    owning_keywords = {kid for term in terms for kid in term["keyword_ids"]}
    assert len(owning_keywords) == 50, "every owning keyword survives de-duplication"
