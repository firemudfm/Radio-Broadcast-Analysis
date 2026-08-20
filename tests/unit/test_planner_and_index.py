"""Station sharing, combined keyword index and capacity admission (ADR-008)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import Database
from app.services.keyword_index import (
    KeywordAlias,
    build_index,
    confirmation_prompt,
    index_from_payload,
    language_hints_for,
)
from app.services.subscription_planner import SubscriptionPlanner

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
STAMP = NOW.isoformat().replace("+00:00", "Z")


def _campaign(
    database: Database,
    campaign_id: str,
    *,
    stations: list[str],
    keywords: list[tuple[str, str, list]],
    status: str = "active",
) -> None:
    def write(connection) -> None:
        connection.execute(
            "INSERT INTO campaigns(id, name, objective, status, monitor_from_utc,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (campaign_id, campaign_id, "brand_mentions", status, STAMP, STAMP, STAMP),
        )
        for station in stations:
            connection.execute(
                "INSERT INTO campaign_stations(campaign_id, station_id) VALUES (?,?)",
                (campaign_id, station),
            )
        for keyword_id, value, aliases in keywords:
            connection.execute(
                "INSERT INTO campaign_keywords(id, campaign_id, entity_id, value, aliases_json,"
                " match_mode, keyword_type, semantic_matching, semantic_threshold, enabled)"
                " VALUES (?,?,?,?,?, 'tokens', 'brand', 0, 0.74, 1)",
                (keyword_id, campaign_id, f"e-{value}", value, json.dumps(aliases)),
            )

    database.write(write)


def _planner(settings: Settings, database: Database, *, now: datetime = NOW) -> SubscriptionPlanner:
    return SubscriptionPlanner(settings, database, clock=lambda: now)


# --- A. station sharing -------------------------------------------------------


def test_three_campaigns_on_one_station_produce_one_subscription(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    """The central invariant: one connection per DISTINCT station."""
    for index in range(3):
        _campaign(
            pipeline_database,
            f"camp-{index}",
            stations=["rb-shared"],
            keywords=[(f"kw-{index}", f"Brand{index}", [])],
        )

    result = _planner(pipeline_settings, pipeline_database).plan_once()

    assert result.unique_requested == 1
    rows = pipeline_database.read_all("SELECT station_id, reference_count FROM station_subscriptions")
    assert len(rows) == 1, "one station must produce exactly one subscription"
    assert int(rows[0]["reference_count"]) == 3
    assert result.reused_station_streams == 1


def test_one_combined_index_holds_every_campaigns_keywords(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    for index in range(3):
        _campaign(
            pipeline_database,
            f"camp-{index}",
            stations=["rb-shared"],
            keywords=[(f"kw-{index}", f"Brand{index}", [])],
        )
    planner = _planner(pipeline_settings, pipeline_database)
    planner.plan_once()

    versions = pipeline_database.read_all(
        "SELECT count(*) AS n FROM station_keyword_index_versions WHERE station_id='rb-shared'"
    )
    assert int(versions[0]["n"]) == 1, "one index per station, not one per campaign-station pair"

    index = planner.keyword_index_for("rb-shared")
    assert index is not None
    assert index.keyword_count == 3
    assert index.campaign_count == 3


def test_no_duplicate_listener_is_requested(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    for index in range(5):
        _campaign(
            pipeline_database,
            f"camp-{index}",
            stations=["rb-shared"],
            keywords=[(f"kw-{index}", "NVIDIA", [])],
        )
    planner = _planner(pipeline_settings, pipeline_database)
    planner.plan_once()
    assert len(planner.assigned_stations(shard_index=0)) == 1


def test_replanning_is_stable(pipeline_settings: Settings, pipeline_database: Database) -> None:
    """A no-op cycle must not churn versions and force listener reloads."""
    _campaign(pipeline_database, "camp-1", stations=["rb-a"], keywords=[("kw-1", "NVIDIA", [])])
    planner = _planner(pipeline_settings, pipeline_database)
    first = planner.plan_once()
    second = planner.plan_once()
    assert first.index_versions_published == 1
    assert second.index_versions_published == 0
    row = pipeline_database.read_one(
        "SELECT max(version) AS v FROM station_keyword_index_versions WHERE station_id='rb-a'"
    )
    assert int(row["v"]) == 1


def test_adding_an_alias_publishes_a_new_version(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    _campaign(pipeline_database, "camp-1", stations=["rb-a"], keywords=[("kw-1", "NVIDIA", [])])
    planner = _planner(pipeline_settings, pipeline_database)
    planner.plan_once()
    pipeline_database.write(
        lambda connection: connection.execute(
            "UPDATE campaign_keywords SET aliases_json=? WHERE id='kw-1'",
            (json.dumps(["एनवीडिया"]),),
        )
    )
    assert planner.plan_once().index_versions_published == 1
    index = planner.keyword_index_for("rb-a")
    assert any("एनवीडिया" in term.display for term in index.terms)


def test_paused_campaign_stops_contributing(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    _campaign(pipeline_database, "camp-1", stations=["rb-a"], keywords=[("kw-1", "A", [])])
    _campaign(pipeline_database, "camp-2", stations=["rb-a"], keywords=[("kw-2", "B", [])])
    planner = _planner(pipeline_settings, pipeline_database)
    planner.plan_once()

    pipeline_database.write(
        lambda connection: connection.execute("UPDATE campaigns SET status='paused' WHERE id='camp-2'")
    )
    planner.plan_once()
    row = pipeline_database.read_one(
        "SELECT reference_count FROM station_subscriptions WHERE station_id='rb-a'"
    )
    assert int(row["reference_count"]) == 1
    assert planner.keyword_index_for("rb-a").keyword_count == 1


# --- wind-down transitions ----------------------------------------------------


def test_one_to_zero_schedules_winddown_after_the_grace_period(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    _campaign(pipeline_database, "camp-1", stations=["rb-a"], keywords=[("kw-1", "A", [])])
    _planner(pipeline_settings, pipeline_database).plan_once()

    pipeline_database.write(
        lambda connection: connection.execute("UPDATE campaigns SET status='paused'")
    )
    _planner(pipeline_settings, pipeline_database).plan_once()
    row = pipeline_database.read_one(
        "SELECT state, winddown_after_utc FROM station_subscriptions WHERE station_id='rb-a'"
    )
    assert str(row["state"]) == "winding_down"
    assert row["winddown_after_utc"] is not None

    # Still inside the grace period: not stopped yet.
    later = _planner(pipeline_settings, pipeline_database, now=NOW + timedelta(seconds=60))
    later.plan_once()
    assert str(
        pipeline_database.read_one(
            "SELECT state FROM station_subscriptions WHERE station_id='rb-a'"
        )["state"]
    ) == "winding_down"

    expired = _planner(pipeline_settings, pipeline_database, now=NOW + timedelta(seconds=400))
    expired.plan_once()
    assert str(
        pipeline_database.read_one(
            "SELECT state FROM station_subscriptions WHERE station_id='rb-a'"
        )["state"]
    ) == "stopped"


def test_references_returning_during_grace_cancel_the_winddown(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    _campaign(pipeline_database, "camp-1", stations=["rb-a"], keywords=[("kw-1", "A", [])])
    _planner(pipeline_settings, pipeline_database).plan_once()
    pipeline_database.write(
        lambda connection: connection.execute("UPDATE campaigns SET status='paused'")
    )
    _planner(pipeline_settings, pipeline_database).plan_once()
    pipeline_database.write(
        lambda connection: connection.execute("UPDATE campaigns SET status='active'")
    )
    _planner(pipeline_settings, pipeline_database, now=NOW + timedelta(seconds=60)).plan_once()

    row = pipeline_database.read_one(
        "SELECT state, winddown_after_utc FROM station_subscriptions WHERE station_id='rb-a'"
    )
    assert str(row["state"]) != "stopped"
    assert row["winddown_after_utc"] is None


def test_a_stopped_station_revives_when_its_campaign_resumes(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    """Regression for the v0.4.1 field bug, in the new planner."""
    _campaign(pipeline_database, "camp-1", stations=["rb-a"], keywords=[("kw-1", "A", [])])
    _planner(pipeline_settings, pipeline_database).plan_once()
    pipeline_database.write(
        lambda connection: connection.execute("UPDATE campaigns SET status='paused'")
    )
    _planner(pipeline_settings, pipeline_database).plan_once()
    _planner(pipeline_settings, pipeline_database, now=NOW + timedelta(seconds=400)).plan_once()
    assert str(
        pipeline_database.read_one("SELECT state FROM station_subscriptions")["state"]
    ) == "stopped"

    pipeline_database.write(
        lambda connection: connection.execute("UPDATE campaigns SET status='active'")
    )
    _planner(pipeline_settings, pipeline_database, now=NOW + timedelta(seconds=500)).plan_once()
    # Revived stations rejoin the rotation pool; the listener grants the
    # actual turn, so any pool state (including waiting) proves the revival.
    assert str(
        pipeline_database.read_one("SELECT state FROM station_subscriptions")["state"]
    ) in {"starting", "active", "pending_capacity"}


# --- capacity -----------------------------------------------------------------


def test_every_requested_station_joins_the_rotation_pool(
    tmp_path, pipeline_database: Database
) -> None:
    """The production bug: candidates[:free] admitted once and never again,
    so a new campaign's stations sat in pending_capacity for days with zero
    mentions. Every referenced station is now assigned to the listener, which
    grants turns; parked means waiting for a turn, never waiting forever."""
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 40,
        RADIO_DATABASE_PATH=tmp_path / "unused.db",
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=3,
        RADIO_LISTENER_MAX_SESSIONS=3,
    )
    for index in range(10):
        _campaign(
            pipeline_database,
            f"camp-{index}",
            stations=[f"rb-station{index}"],
            keywords=[(f"kw-{index}", "NVIDIA", [])],
        )
    planner = _planner(settings, pipeline_database)
    result = planner.plan_once()

    assert result.unique_requested == 10
    assert result.pending_capacity == 10, "nothing streams until the listener grants a turn"
    parked = pipeline_database.read_all(
        "SELECT station_id, state_reason FROM station_subscriptions WHERE state='pending_capacity'"
    )
    assert len(parked) == 10
    assert "listening turn" in str(parked[0]["state_reason"])
    # THE point: all ten reach the listener; none is invisible to rotation.
    assigned = planner.assigned_stations(shard_index=0)
    assert len(assigned) == 10


def test_freed_slots_are_promoted(tmp_path, pipeline_database: Database) -> None:
    settings = Settings(
        RADIO_S3_BUCKET="b",
        RADIO_AUDIO_TOKEN_SECRET="x" * 40,
        RADIO_DATABASE_PATH=tmp_path / "unused.db",
        RADIO_MAX_ACTIVE_UNIQUE_STATIONS=2,
        RADIO_LISTENER_MAX_SESSIONS=2,
    )
    for index in range(4):
        _campaign(
            pipeline_database,
            f"camp-{index}",
            stations=[f"rb-station{index}"],
            keywords=[(f"kw-{index}", "NVIDIA", [])],
        )
    _planner(settings, pipeline_database).plan_once()

    # A campaign goes away; its station winds down and leaves the pool.
    pipeline_database.write(
        lambda connection: connection.execute("DELETE FROM campaigns WHERE id='camp-0'")
    )
    _planner(settings, pipeline_database).plan_once()
    result = _planner(settings, pipeline_database, now=NOW + timedelta(seconds=400)).plan_once()
    assert result.unique_requested == 3
    assert result.pending_capacity == 3, "the remaining stations wait for listening turns"


def test_capacity_counters_are_distinct(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    """Many campaigns, few stations: the counters must not be interchangeable."""
    for index in range(6):
        _campaign(
            pipeline_database,
            f"camp-{index}",
            stations=["rb-a", "rb-b"],
            keywords=[(f"kw-{index}", f"Brand{index}", [])],
        )
    planner = _planner(pipeline_settings, pipeline_database)
    planner.plan_once()
    snapshot = planner.capacity_snapshot()

    assert snapshot["campaign_station_reference_count"] == 12  # 6 campaigns x 2 stations
    assert snapshot["unique_requested_station_count"] == 2
    # Nothing streams until the listener grants turns; both wait in the pool.
    assert snapshot["unique_active_station_count"] == 0
    assert snapshot["reused_station_stream_count"] == 2
    assert snapshot["pending_capacity_station_count"] == 2
    assert snapshot["active_unique_station_limit"] == 8


def test_unsafe_station_ids_are_skipped_not_sanitised(
    pipeline_settings: Settings, pipeline_database: Database
) -> None:
    """A silently rewritten id would attribute mentions to the wrong station."""
    _campaign(
        pipeline_database, "camp-1", stations=["../etc/passwd", "rb-ok"], keywords=[("kw-1", "A", [])]
    )
    result = _planner(pipeline_settings, pipeline_database).plan_once()
    assert result.unique_requested == 1
    rows = pipeline_database.read_all("SELECT station_id FROM station_subscriptions")
    assert [str(row["station_id"]) for row in rows] == ["rb-ok"]


# --- keyword index ------------------------------------------------------------


def test_shared_terms_map_to_multiple_keywords() -> None:
    """Two campaigns tracking NVIDIA scan the text once, not twice."""
    index = build_index(
        "rb-a",
        [
            {"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "NVIDIA"},
            {"keyword_id": "kw-2", "campaign_id": "c2", "canonical_value": "NVIDIA"},
        ],
    )
    terms = [term for term in index.terms if term.normalized == "nvidia"]
    assert len(terms) == 1
    assert terms[0].keyword_ids == ("kw-1", "kw-2")
    assert index.campaigns_for("kw-1") == ("c1",)


def test_index_preserves_full_provenance() -> None:
    index = build_index(
        "rb-a",
        [
            {
                "keyword_id": "kw-1",
                "campaign_id": "c1",
                "entity_id": "api-nvidia-abc",
                "canonical_value": "NVIDIA",
                "keyword_type": "brand",
                "aliases": [{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}],
                "content_policy": {"include_song_lyrics": False},
            }
        ],
    )
    entry = index.entry("kw-1")
    assert entry.entity_id == "api-nvidia-abc"
    assert entry.keyword_type == "brand"
    assert entry.is_strict_entity is True
    assert entry.aliases[0].kind == "native_script"
    assert entry.aliases[0].language == "hi"
    assert entry.content_policy["include_song_lyrics"] is False


def test_fingerprint_ignores_ordering_but_not_content() -> None:
    a = build_index(
        "rb-a",
        [
            {"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "A"},
            {"keyword_id": "kw-2", "campaign_id": "c1", "canonical_value": "B"},
        ],
    )
    b = build_index(
        "rb-a",
        [
            {"keyword_id": "kw-2", "campaign_id": "c1", "canonical_value": "B"},
            {"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "A"},
        ],
    )
    assert a.fingerprint == b.fingerprint

    changed = build_index(
        "rb-a",
        [
            {"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "A", "aliases": ["A1"]},
            {"keyword_id": "kw-2", "campaign_id": "c1", "canonical_value": "B"},
        ],
    )
    assert changed.fingerprint != a.fingerprint


def test_longer_terms_are_ordered_first() -> None:
    """So the matcher reports 'NVIDIA RTX' rather than the 'NVIDIA' prefix."""
    index = build_index(
        "rb-a",
        [
            {"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "NVIDIA"},
            {"keyword_id": "kw-2", "campaign_id": "c1", "canonical_value": "NVIDIA RTX"},
        ],
    )
    normalized = [term.normalized for term in index.terms]
    assert normalized.index("nvidia rtx") < normalized.index("nvidia")


def test_legacy_flat_aliases_still_work() -> None:
    """Backward compatibility with the existing aliases_json shape."""
    index = build_index(
        "rb-a",
        [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "NVIDIA",
          "aliases": ["Nvidia Corp", "एनवीडिया"]}],
    )
    displays = {term.display for term in index.terms}
    assert {"NVIDIA", "Nvidia Corp", "एनवीडिया"} <= displays


def test_payload_round_trip() -> None:
    original = build_index(
        "rb-a",
        [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "NVIDIA",
          "aliases": [{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}]}],
    )
    restored = index_from_payload(json.loads(json.dumps(original.to_payload())))
    assert restored.fingerprint == original.fingerprint
    assert restored.keyword_count == original.keyword_count
    assert {t.normalized for t in restored.terms} == {t.normalized for t in original.terms}


def test_language_hints_prefer_station_languages() -> None:
    index = build_index(
        "rb-a",
        [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "NVIDIA",
          "aliases": [{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}]}],
    )
    assert language_hints_for(index, ["mr", "en"])[:2] == ["mr", "en"]
    assert "hi" in language_hints_for(index, ["mr"])


def test_confirmation_prompt_is_capped_and_deduplicated() -> None:
    """A keyword must not be able to inject arbitrary text into the decoder."""
    index = build_index(
        "rb-a",
        [
            {"keyword_id": f"kw-{i}", "campaign_id": "c1", "canonical_value": f"Brand{i}"}
            for i in range(200)
        ]
        + [{"keyword_id": "kw-dupe", "campaign_id": "c2", "canonical_value": "Brand0"}],
    )
    prompt = confirmation_prompt(index, max_characters=100)
    assert len(prompt) <= 100
    assert prompt.count("Brand0") == 1
    assert confirmation_prompt(index, max_characters=0) == ""


def test_overlong_alias_values_are_truncated() -> None:
    index = build_index(
        "rb-a",
        [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "A",
          "aliases": ["z" * 5000]}],
    )
    assert all(len(alias.value) <= 200 for entry in index.entries for alias in entry.aliases)


def test_alias_count_is_bounded() -> None:
    index = build_index(
        "rb-a",
        [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "A",
          "aliases": [f"alias-{i}" for i in range(1000)]}],
    )
    assert len(index.entry("kw-1").aliases) <= 200


@pytest.mark.parametrize("kind", ["brand", "person", "product", "organization"])
def test_named_entity_types_are_strict(kind: str) -> None:
    index = build_index(
        "rb-a", [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "X",
                  "keyword_type": kind}]
    )
    assert index.entry("kw-1").is_strict_entity is True


@pytest.mark.parametrize("kind", ["topic", "concept"])
def test_concept_types_are_not_strict(kind: str) -> None:
    index = build_index(
        "rb-a", [{"keyword_id": "kw-1", "campaign_id": "c1", "canonical_value": "X",
                  "keyword_type": kind}]
    )
    assert index.entry("kw-1").is_strict_entity is False


def test_alias_normalized_forms_cover_script_variants() -> None:
    """Hindi keeps its matras; Latin also gets a diacritic-folded form."""
    hindi = KeywordAlias(value="किताब").normalized_forms()
    assert any("ि" in form for form in hindi), "matras must survive normalisation"
    latin = KeywordAlias(value="Café").normalized_forms()
    assert "cafe" in latin
