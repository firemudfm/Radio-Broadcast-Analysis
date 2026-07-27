#!/bin/sh
# API entrypoint.
#
# Migrations run here rather than in a separate init container: the API owns the
# schema (app/db.py connects and migrates on start-up), and a worker that races
# ahead of an unmigrated database fails in a much less obvious way than one that
# waits for the API to be healthy.
set -eu

: "${RADIO_API_PORT:=8788}"
: "${RADIO_API_WORKERS:=1}"

# One uvicorn worker by default. The control plane is I/O bound and SQLite has a
# single writer; extra processes would contend for that write lock rather than
# adding throughput.
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${RADIO_API_PORT}" \
    --workers "${RADIO_API_WORKERS}" \
    --no-server-header \
    --proxy-headers
