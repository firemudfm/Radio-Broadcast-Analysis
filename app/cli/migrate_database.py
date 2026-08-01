"""Standalone database migration entrypoint.

    python -m app.cli.migrate_database [--database PATH] [--check-only]

Deliberately separate from ``app/__main__.py``. That module starts uvicorn, and
running migrations by "starting the app and hoping the lifespan gets far
enough" means a deploy cannot tell a migration failure from a bind failure, and
leaves an HTTP socket open on a host that is mid-deploy.

This module reuses the **same** ``Database`` class and the **same**
``run_migrations`` implementation as normal start-up. It is a different
entrypoint, not a second migration engine -- a parallel implementation would
drift from the one that actually runs in production.

Explicitly does not: start an HTTP server, open a radio stream, start an SQS
consumer, call the LLM, load an ASR model, or mutate S3. The only side effect
is the schema of the SQLite file it is pointed at.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import Database
from ..migrations import applied_versions, current_version

#: Distinct exit codes so a deploy script can branch without parsing stdout.
EXIT_OK = 0
EXIT_MIGRATION_FAILED = 1
EXIT_INTEGRITY_FAILED = 2
EXIT_BAD_USAGE = 64


def _report(payload: dict[str, Any]) -> None:
    """Emit one JSON object. No secrets: only paths, versions and counts."""
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.migrate_database",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite path. Defaults to RADIO_DATABASE_PATH from configuration.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Report the current schema version without applying anything.",
    )
    parser.add_argument(
        "--skip-integrity-check",
        action="store_true",
        help="Skip PRAGMA integrity_check (it is O(database size)).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Configuration is preferred but not always required. Without --database
    # there is no way to know which file to migrate, so a bad config is fatal.
    # With --database the caller has already answered the only question that
    # settings were needed for, and the remaining three values are SQLite
    # tunings that Database already defaults. Requiring RADIO_S3_BUCKET and an
    # audio-token secret before a schema migration would couple recovery to
    # unrelated configuration -- exactly when an operator is least able to
    # supply it.
    settings = None
    try:
        settings = get_settings()
    except Exception as error:  # noqa: BLE001 - a bad config is a usage error
        if args.database is None:
            print(f"configuration error: {type(error).__name__}: {error}", file=sys.stderr)
            print(
                "hint: pass --database PATH to migrate without full application "
                "configuration",
                file=sys.stderr,
            )
            return EXIT_BAD_USAGE
        print(
            f"configuration unavailable ({type(error).__name__}); "
            "continuing with SQLite defaults because --database was given",
            file=sys.stderr,
        )

    database_path = Path(args.database) if args.database else Path(settings.RADIO_DATABASE_PATH)  # type: ignore[union-attr]

    if args.check_only and not database_path.exists():
        _report({"status": "ABSENT", "database": str(database_path), "schema_version": None})
        return EXIT_OK

    tunings: dict[str, Any] = {}
    if settings is not None:
        tunings = {
            "mention_window_days": settings.RADIO_MENTION_WINDOW_DAYS,
            "mention_audio_pad_seconds": settings.RADIO_MENTION_AUDIO_PAD_SECONDS,
            "busy_retries": settings.RADIO_SQLITE_BUSY_RETRIES,
        }
    database = Database(database_path, **tunings)

    try:
        # Database.connect() applies the schema and runs the versioned
        # migrations -- the same call path the API uses at start-up.
        before = _schema_version_if_present(database_path)
        if args.check_only:
            _report(
                {
                    "status": "PASS",
                    "checked_only": True,
                    "database": str(database_path),
                    "schema_version": before,
                }
            )
            return EXIT_OK

        database.connect()
    except sqlite3.Error as error:
        print(f"migration failed: {error}", file=sys.stderr)
        return EXIT_MIGRATION_FAILED
    except Exception as error:  # noqa: BLE001 - report, never traceback-dump
        print(f"migration failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_MIGRATION_FAILED

    try:
        connection = database._conn()  # noqa: SLF001 - same-process connection by design
        after = current_version(connection)
        applied = applied_versions(connection)

        payload: dict[str, Any] = {
            "status": "PASS",
            "database": str(database_path),
            "schema_version_before": before,
            "schema_version_after": after,
            "migrations_applied": sorted(applied),
            "pragmas": {
                name: database.pragma(name)
                for name in ("journal_mode", "foreign_keys", "busy_timeout", "synchronous")
            },
        }

        if not args.skip_integrity_check:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = str(result[0]) if result else "unknown"
            payload["integrity_check"] = integrity
            if integrity != "ok":
                payload["status"] = "FAIL"
                _report(payload)
                print(f"integrity check failed: {integrity}", file=sys.stderr)
                return EXIT_INTEGRITY_FAILED

        _report(payload)
        return EXIT_OK
    except sqlite3.Error as error:
        print(f"post-migration verification failed: {error}", file=sys.stderr)
        return EXIT_INTEGRITY_FAILED
    finally:
        database.close()


def _schema_version_if_present(path: Path) -> int | None:
    """Read the schema version without creating or migrating the file."""
    if not path.exists():
        return None
    try:
        # Read-only URI: inspecting the "before" state must not create the file
        # or take a write lock on a database another process may be using.
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        return current_version(connection)
    except sqlite3.Error:
        return None
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
