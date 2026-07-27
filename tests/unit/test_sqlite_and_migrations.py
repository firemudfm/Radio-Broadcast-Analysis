"""SQLite pragmas, migrations, uniqueness and busy retry (ADR-004)."""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db import CONNECTION_PRAGMAS, Database, configure_connection, is_busy_error, retry_on_busy
from app.migrations import Migration, MigrationError, current_version, run_migrations
from app.migrations.registry import MIGRATIONS

STAMP = datetime(2026, 7, 27, tzinfo=UTC).isoformat()


# --- pragmas ------------------------------------------------------------------


def test_every_required_pragma_is_set(pipeline_database: Database) -> None:
    assert str(pipeline_database.pragma("journal_mode")).lower() == "wal"
    assert int(pipeline_database.pragma("foreign_keys")) == 1
    assert int(pipeline_database.pragma("busy_timeout")) == 30_000


def test_synchronous_is_normal_not_the_sqlite_default(pipeline_database: Database) -> None:
    """Direct regression test for the gap found in the baseline audit.

    `synchronous` is per-connection; it was never set anywhere, so every
    process ran at FULL. 1 == NORMAL.
    """
    assert int(pipeline_database.pragma("synchronous")) == 1


def test_pragmas_apply_to_a_freshly_opened_connection(tmp_path: Path) -> None:
    """Per-connection pragmas do not inherit; a new worker must set its own."""
    path = tmp_path / "radio.db"
    Database(path).connect()
    raw = sqlite3.connect(path)
    assert int(raw.execute("PRAGMA synchronous").fetchone()[0]) == 2, "expected the SQLite default"
    configure_connection(raw)
    assert int(raw.execute("PRAGMA synchronous").fetchone()[0]) == 1
    assert int(raw.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    raw.close()


def test_connection_pragma_list_is_complete() -> None:
    assert {name for name, _ in CONNECTION_PRAGMAS} == {
        "journal_mode",
        "foreign_keys",
        "busy_timeout",
        "synchronous",
    }


# --- migrations ---------------------------------------------------------------


def test_migrations_are_applied(pipeline_database: Database) -> None:
    assert current_version(pipeline_database._conn()) == max(m.version for m in MIGRATIONS)


def test_migrations_are_idempotent(pipeline_database: Database) -> None:
    assert run_migrations(pipeline_database._conn()) == []


def test_expected_tables_exist(pipeline_database: Database) -> None:
    rows = pipeline_database.read_all("SELECT name FROM sqlite_master WHERE type='table'")
    names = {str(row["name"]) for row in rows}
    required = {
        "schema_migrations",
        "station_subscriptions",
        "station_keyword_bindings",
        "station_keyword_index_versions",
        "station_sessions",
        "audio_segments",
        "transcription_jobs",
        "transcripts",
        "conversation_sessions",
        "mention_events",
        "mention_campaigns",
        "mention_keywords",
        "analysis_jobs",
        "analysis_results",
        "outbox_events",
        "inbox_messages",
        "worker_heartbeats",
        "processing_failures",
    }
    assert required <= names


def test_legacy_tables_are_untouched(pipeline_database: Database) -> None:
    """The baseline schema must survive the migration runner unchanged."""
    rows = pipeline_database.read_all("SELECT name FROM sqlite_master WHERE type='table'")
    names = {str(row["name"]) for row in rows}
    assert {"campaigns", "campaign_keywords", "mentions", "mention_analysis"} <= names


def test_a_modified_applied_migration_is_detected(pipeline_database: Database) -> None:
    original = MIGRATIONS[0]
    tampered = [
        Migration(original.version, original.name, original.statements + ("SELECT 1",)),
        *MIGRATIONS[1:],
    ]
    with pytest.raises(MigrationError, match="modified after it was applied"):
        run_migrations(pipeline_database._conn(), tampered)


def test_duplicate_versions_are_rejected(pipeline_database: Database) -> None:
    with pytest.raises(MigrationError, match="Duplicate migration versions"):
        run_migrations(pipeline_database._conn(), [MIGRATIONS[0], MIGRATIONS[0]])


def test_integrity_check_passes(pipeline_database: Database) -> None:
    assert str(pipeline_database.pragma("integrity_check")) == "ok"


# --- constraints --------------------------------------------------------------


def test_foreign_keys_are_enforced(pipeline_database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        pipeline_database.write(
            lambda connection: connection.execute(
                "INSERT INTO mention_campaigns(mention_id, campaign_id, included, created_at_utc)"
                " VALUES ('no-such-mention','c',1,?)",
                (STAMP,),
            )
        )


def _seed_mention(database: Database, mention_id: str = "m1") -> None:
    def write(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO conversation_sessions(conversation_id, station_id, station_session_id,"
            " first_sequence_number, last_sequence_number, started_at_utc, trace_id,"
            " created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"c-{mention_id}", "rb-abc", "s1", 1, 2, STAMP, "t", STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO mention_events(mention_id, conversation_id, station_id,"
            " broadcast_start_utc, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,?,?,?,?)",
            (mention_id, f"c-{mention_id}", "rb-abc", STAMP, "t", STAMP, STAMP),
        )

    database.write(write)


@pytest.mark.parametrize(
    ("table", "columns", "values"),
    [
        (
            "mention_campaigns",
            "(mention_id, campaign_id, included, created_at_utc)",
            ("m1", "camp-1", 1, STAMP),
        ),
        (
            "mention_keywords",
            "(mention_id, keyword_id, campaign_id, canonical_value, matched_text,"
            " match_level, created_at_utc)",
            ("m1", "kw-1", "camp-1", "NVIDIA", "NVIDIA", "exact", STAMP),
        ),
    ],
)
def test_mapping_tables_reject_duplicates(
    pipeline_database: Database, table: str, columns: str, values: tuple
) -> None:
    _seed_mention(pipeline_database)
    placeholders = ",".join("?" * len(values))
    statement = f"INSERT INTO {table}{columns} VALUES ({placeholders})"  # nosec B608 (test fixture)
    pipeline_database.write(lambda connection: connection.execute(statement, values))
    with pytest.raises(sqlite3.IntegrityError):
        pipeline_database.write(lambda connection: connection.execute(statement, values))


def test_one_conversation_yields_at_most_one_mention(pipeline_database: Database) -> None:
    """UNIQUE(conversation_id) is what makes 'one analysis per conversation' true."""
    _seed_mention(pipeline_database)
    with pytest.raises(sqlite3.IntegrityError):
        pipeline_database.write(
            lambda connection: connection.execute(
                "INSERT INTO mention_events(mention_id, conversation_id, station_id,"
                " broadcast_start_utc, trace_id, created_at_utc, updated_at_utc)"
                " VALUES ('m2','c-m1','rb-abc',?,?,?,?)",
                (STAMP, "t", STAMP, STAMP),
            )
        )


def test_inbox_uniqueness_prevents_reprocessing(pipeline_database: Database) -> None:
    statement = (
        "INSERT INTO inbox_messages(queue_name, message_deduplication_id, status,"
        " first_seen_at_utc) VALUES ('q','d','processed',?)"
    )
    pipeline_database.write(lambda connection: connection.execute(statement, (STAMP,)))
    with pytest.raises(sqlite3.IntegrityError):
        pipeline_database.write(lambda connection: connection.execute(statement, (STAMP,)))


def test_outbox_uniqueness_is_per_queue(pipeline_database: Database) -> None:
    def insert(queue_name: str):
        return lambda connection: connection.execute(
            "INSERT INTO outbox_events(event_id, queue_name, message_group_id,"
            " message_deduplication_id, payload_json, available_at_utc, created_at_utc,"
            " updated_at_utc) VALUES (?,?,?,?,?,?,?,?)",
            (f"e-{queue_name}", queue_name, "g", "same-dedup", "{}", STAMP, STAMP, STAMP),
        )

    pipeline_database.write(insert("t.fifo"))
    pipeline_database.write(insert("a.fifo"))  # different queue: allowed
    with pytest.raises(sqlite3.IntegrityError):
        pipeline_database.write(insert("t.fifo"))


def test_segment_sequence_is_unique_per_session(pipeline_database: Database) -> None:
    def insert(segment_id: str):
        return lambda connection: connection.execute(
            "INSERT INTO audio_segments(segment_id, station_id, station_session_id,"
            " sequence_number, started_at_utc, ended_at_utc, duration_ms, content_class,"
            " storage_backend, sha256, size_bytes, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (segment_id, "rb-abc", "sess-1", 5, STAMP, STAMP, 20000, "speech",
             "local", "a" * 64, 1024, "t", STAMP, STAMP),
        )

    pipeline_database.write(insert("seg-1"))
    with pytest.raises(sqlite3.IntegrityError):
        pipeline_database.write(insert("seg-2"))


# --- busy handling ------------------------------------------------------------


def test_busy_errors_are_retried() -> None:
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_on_busy(flaky, sleep=lambda _: None) == "ok"
    assert attempts["n"] == 3


def test_non_busy_errors_are_not_retried() -> None:
    """A retry loop that swallows real errors is worse than no retry loop."""
    attempts = {"n": 0}

    def broken():
        attempts["n"] += 1
        raise sqlite3.OperationalError("no such column: nope")

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        retry_on_busy(broken, sleep=lambda _: None)
    assert attempts["n"] == 1


def test_retries_are_bounded() -> None:
    attempts = {"n": 0}

    def always_busy():
        attempts["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_on_busy(always_busy, retries=2, sleep=lambda _: None)
    assert attempts["n"] == 3


def test_is_busy_error_ignores_other_exception_types() -> None:
    assert is_busy_error(ValueError("database is locked")) is False
    assert is_busy_error(sqlite3.OperationalError("database is busy")) is True


def test_concurrent_short_writes_all_commit(pipeline_database: Database) -> None:
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            pipeline_database.write(
                lambda connection: connection.execute(
                    "INSERT INTO processing_failures(component, error_code, retryable, message,"
                    " created_at_utc) VALUES ('concurrency','ok',0,?,?)",
                    (f"row-{index}", STAMP),
                )
            )
        except BaseException as error:  # noqa: BLE001 - collected and asserted
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = pipeline_database.read_all(
        "SELECT count(*) AS n FROM processing_failures WHERE component='concurrency'"
    )
    assert int(rows[0]["n"]) == 12
    assert str(pipeline_database.pragma("integrity_check")) == "ok"
