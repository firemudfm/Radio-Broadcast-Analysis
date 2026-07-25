#!/usr/bin/env bash
# Post-upgrade audit for backend v0.4.0. Run on the EC2 instance as any user.
set -euo pipefail
PORT="${RADIO_API_PORT:-8788}"
BASE="http://127.0.0.1:${PORT}"

check() { printf '%-58s' "$1"; shift; if "$@" >/dev/null 2>&1; then echo PASS; else echo FAIL; fi; }

echo "== FireMud backend v0.4.0 audit =="
check "healthz reports 0.4.0" bash -c "curl -sf ${BASE}/healthz | grep -q '\"version\":\"0.4.0\"'"
check "runtime endpoint" curl -sf "${BASE}/api/v1/brand-signal/runtime"
check "catalogue countries" curl -sf "${BASE}/api/v1/radio-catalog/countries"
check "catalogue station search (DE)" curl -sf "${BASE}/api/v1/radio-catalog/stations?country_code=DE&limit=3"
check "monitoring capacity" curl -sf "${BASE}/api/v1/monitoring/capacity"
check "managed stations list" curl -sf "${BASE}/api/v1/monitoring/stations"
check "legacy campaigns endpoint" curl -sf "${BASE}/api/v1/brand-signal/campaigns"
check "legacy dashboard endpoint" curl -sf "${BASE}/api/v1/brand-signal/dashboard"
check "no stream URLs in catalogue" bash -c "! curl -sf '${BASE}/api/v1/radio-catalog/stations?country_code=DE&limit=20' | grep -qE 'url_resolved|stream_url'"
check "reconciler service active" systemctl is-active --quiet radio-station-reconciler
check "API service active" systemctl is-active --quiet radio-intelligence-api
check "hertz879 capture active" systemctl is-active --quiet radio-capture@hertz879
check "hertz879 uploader active" systemctl is-active --quiet radio-uploader@hertz879
check "hertz879 worker active" systemctl is-active --quiet radio-pipeline-worker@hertz879
echo "== audit complete =="
