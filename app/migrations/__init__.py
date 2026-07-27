"""Versioned SQLite migrations (ADR-004).

Replaces ad hoc ``CREATE TABLE IF NOT EXISTS`` scattered across modules for all
*new* tables. The pre-existing schemas in ``app/db.py`` and
``app/db_catalog.py`` are deliberately left alone: they are already idempotent,
they are covered by the baseline test suite, and rewriting them would risk a
live pilot for no functional gain. The runner records them as baseline versions
0001 and 0002 when it finds their tables present.

Properties the runner guarantees:

* **Ordered** — applied strictly by ascending version.
* **Idempotent** — a second run is a no-op.
* **Atomic per migration** — each runs inside one transaction.
* **Tamper-evident** — a checksum over the statements detects a migration file
  edited after it was applied, which is otherwise a silent schema divergence
  between hosts.
* **Mode-independent** — migrations run in both pipeline modes, so switching
  ``RADIO_PIPELINE_MODE`` is never a data-migration event (ADR-001).

Migrations are forward-only by policy. Each carries a documented manual
``DOWN`` in its module docstring for emergency use.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at_utc TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        """Stable digest over the normalised statements.

        Whitespace is collapsed so that reformatting a migration does not
        register as tampering, while a real change to the SQL does.
        """
        hasher = hashlib.sha256()
        for statement in self.statements:
            hasher.update(" ".join(statement.split()).encode("utf-8"))
            hasher.update(b";")
        return hasher.hexdigest()


class MigrationError(RuntimeError):
    """A migration could not be applied, or history is inconsistent."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def applied_versions(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """Return ``{version: (name, checksum)}`` already applied."""
    connection.executescript(SCHEMA_MIGRATIONS_DDL)
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations"
    ).fetchall()
    return {int(row[0]): (str(row[1]), str(row[2])) for row in rows}


def _adopt_baseline(connection: sqlite3.Connection, applied: dict[int, tuple[str, str]]) -> None:
    """Record pre-migration schemas as versions 0001/0002 when present.

    Without this, a database created by the legacy code would look like it was
    missing every migration, and version 0003 would run against tables whose
    existence it assumes.
    """
    existing = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    baselines = (
        (1, "baseline_v03_core", "campaigns"),
        (2, "baseline_v04_catalog", "managed_stations"),
    )
    for version, name, marker_table in baselines:
        if version in applied or marker_table not in existing:
            continue
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc)"
            " VALUES (?, ?, ?, ?)",
            (version, name, "baseline", _utc_now_iso()),
        )
        applied[version] = (name, "baseline")
        logger.info("Adopted existing schema as migration %04d (%s)", version, name)


def run_migrations(connection: sqlite3.Connection, migrations=None) -> list[int]:
    """Apply every pending migration in order. Returns the versions applied.

    Two containers may race here. Each migration takes the SQLite write lock for
    its own transaction, so the loser simply finds the version already recorded
    on its next iteration and skips it.
    """
    from .registry import MIGRATIONS

    pending_source = tuple(MIGRATIONS if migrations is None else migrations)
    ordered = sorted(pending_source, key=lambda item: item.version)

    versions = [item.version for item in ordered]
    if len(set(versions)) != len(versions):
        duplicates = sorted({v for v in versions if versions.count(v) > 1})
        raise MigrationError(f"Duplicate migration versions: {duplicates}")

    applied = applied_versions(connection)
    _adopt_baseline(connection, applied)
    connection.commit()

    executed: list[int] = []
    for migration in ordered:
        recorded = applied.get(migration.version)
        if recorded is not None:
            name, checksum = recorded
            if checksum not in {migration.checksum, "baseline"}:
                raise MigrationError(
                    f"Migration {migration.version:04d} ({name}) was modified after it was "
                    f"applied: recorded checksum {checksum[:12]}… does not match "
                    f"{migration.checksum[:12]}…. Add a new migration instead of editing "
                    f"an applied one."
                )
            continue
        logger.info("Applying migration %04d %s", migration.version, migration.name)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc)"
                " VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, _utc_now_iso()),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            # Another process applied this version between our read and write.
            connection.rollback()
            logger.info("Migration %04d already applied by another process", migration.version)
            continue
        except Exception as error:
            connection.rollback()
            raise MigrationError(
                f"Migration {migration.version:04d} ({migration.name}) failed: {error}"
            ) from error
        executed.append(migration.version)
    return executed


def current_version(connection: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 for an empty database."""
    connection.executescript(SCHEMA_MIGRATIONS_DDL)
    row = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


__all__ = [
    "SCHEMA_MIGRATIONS_DDL",
    "Migration",
    "MigrationError",
    "applied_versions",
    "current_version",
    "run_migrations",
]
