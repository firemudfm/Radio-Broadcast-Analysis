#!/usr/bin/env python3
"""Container health check for the pipeline workers.

A worker has no HTTP surface, so liveness is read from the heartbeat row it
writes to SQLite. That is a better signal than "the process exists": a worker
wedged inside a hung network call still has a live PID but stops beating.

The role is taken from ``RADIO_WORKER_ROLE`` (Compose sets it per service).
Stdlib ``sqlite3`` only -- no application import, so the probe cannot fail for
configuration reasons unrelated to worker liveness.

Exit 0 = healthy, 1 = unhealthy.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime

DEFAULT_DATABASE = "/var/lib/radio/database/radio.db"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main() -> int:
    role = os.environ.get("RADIO_WORKER_ROLE", "").strip()
    if not role:
        print("RADIO_WORKER_ROLE is not set", file=sys.stderr)
        return 1

    database_path = os.environ.get("RADIO_DATABASE_PATH", DEFAULT_DATABASE)
    stale_after = int(os.environ.get("RADIO_HEARTBEAT_STALE_SECONDS", "120"))

    try:
        # Read-only URI: a health probe must never be able to write, and must
        # never create the database file if the path is wrong.
        connection = sqlite3.connect(
            f"file:{database_path}?mode=ro", uri=True, timeout=3
        )
    except sqlite3.Error as error:
        print(f"database unreachable: {error}", file=sys.stderr)
        return 1

    try:
        row = connection.execute(
            "SELECT status, last_seen_utc FROM worker_heartbeats"
            " WHERE role=? ORDER BY last_seen_utc DESC LIMIT 1",
            (role,),
        ).fetchone()
    except sqlite3.Error as error:
        print(f"heartbeat query failed: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if row is None:
        print(f"no heartbeat for role {role!r}", file=sys.stderr)
        return 1

    status, last_seen = str(row[0]), _parse(row[1])
    if last_seen is None:
        print("heartbeat timestamp is unreadable", file=sys.stderr)
        return 1

    age = (datetime.now(UTC) - last_seen).total_seconds()
    if age > stale_after:
        print(f"heartbeat is {age:.0f}s old (limit {stale_after}s)", file=sys.stderr)
        return 1
    if status == "stopped":
        print("worker reports stopped", file=sys.stderr)
        return 1
    # `degraded` is healthy for this purpose: the worker is alive and reporting
    # a real condition (spool pressure, say). Restarting it would not help and
    # would drop whatever it is holding.
    return 0


if __name__ == "__main__":
    sys.exit(main())
