#!/usr/bin/env bash
# Post-deploy smoke test against a running stack.
#
# Checks that the deployment can actually do its job, not merely that processes
# exist. A container can be "up" while holding a stale keyword index, a full
# spool, or no listener at all -- so this reads the endpoints that would reveal
# each of those.
#
#   scripts/smoke-test.sh                        # http://127.0.0.1:8788
#   scripts/smoke-test.sh http://host:8788
#
# Read-only: it creates no campaign and writes nothing. Safe against production.
set -uo pipefail

BASE_URL="${1:-${RADIO_API_URL:-http://127.0.0.1:8788}}"
TIMEOUT="${RADIO_SMOKE_TIMEOUT:-10}"
FAILURES=0

pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1" >&2; FAILURES=$((FAILURES + 1)); }

fetch() {
    curl -fsS --max-time "${TIMEOUT}" "$1" 2>/dev/null
}

status_of() {
    curl -s -o /dev/null -w '%{http_code}' --max-time "${TIMEOUT}" "$1" 2>/dev/null
}

json_field() {
    python3 -c "
import json,sys
try:
    document = json.load(sys.stdin)
except Exception:
    print('')
    sys.exit(0)
for key in sys.argv[1].split('.'):
    if isinstance(document, dict):
        document = document.get(key)
    else:
        document = None
    if document is None:
        break
print('' if document is None else document)
" "$1"
}

echo "==> Smoke testing ${BASE_URL}"

# --- liveness ----------------------------------------------------------------

HEALTH="$(fetch "${BASE_URL}/healthz")"
if [ -z "${HEALTH}" ]; then
    fail "/healthz did not respond"
    echo; echo "smoke-test: FAILED (the API is unreachable)" >&2
    exit 1
fi
pass "/healthz responded"

DATABASE="$(printf '%s' "${HEALTH}" | json_field database)"
[ "${DATABASE}" = "ok" ] && pass "database: ok" || fail "database: ${DATABASE:-unknown}"

AUTH_MODE="$(printf '%s' "${HEALTH}" | json_field auth_mode)"
# The pilot is intentionally unauthenticated; a change here is a contract break
# for the frontend, so it is asserted rather than assumed.
[ "${AUTH_MODE}" = "none" ] && pass "auth_mode: none (unchanged)" \
    || fail "auth_mode changed to '${AUTH_MODE}'"

MODE="$(printf '%s' "${HEALTH}" | json_field pipeline_mode)"
echo "  INFO  pipeline_mode: ${MODE:-legacy}"

# --- readiness ---------------------------------------------------------------

READY_CODE="$(status_of "${BASE_URL}/readyz")"
READY_BODY="$(curl -s --max-time "${TIMEOUT}" "${BASE_URL}/readyz" 2>/dev/null)"
READY="$(printf '%s' "${READY_BODY}" | json_field ready)"
if [ "${READY_CODE}" = "200" ] && [ "${READY}" = "True" ]; then
    pass "/readyz: ready"
else
    fail "/readyz: HTTP ${READY_CODE} ready=${READY:-unknown} -- ${READY_BODY}"
fi

# --- pipeline ----------------------------------------------------------------

if [ "${MODE}" = "shared_sqs" ]; then
    PIPELINE="$(fetch "${BASE_URL}/api/v1/monitoring/pipeline")"
    if [ -z "${PIPELINE}" ]; then
        fail "/api/v1/monitoring/pipeline did not respond"
    else
        pass "/api/v1/monitoring/pipeline responded"
        for component in listener transcription_worker analysis_worker planner; do
            STATE="$(printf '%s' "${PIPELINE}" | json_field "components.${component}")"
            [ "${STATE}" = "ok" ] && pass "${component}: ok" \
                || fail "${component}: ${STATE:-absent}"
        done

        SPOOL="$(printf '%s' "${PIPELINE}" | json_field spool_pressure)"
        case "${SPOOL}" in
            ok)              pass "spool: ok" ;;
            warning)         echo "  WARN  spool above the warning watermark" ;;
            pause|emergency) fail "spool pressure: ${SPOOL}" ;;
            *)               fail "spool pressure unknown" ;;
        esac

        ACTIVE="$(printf '%s' "${PIPELINE}" | json_field unique_active_station_count)"
        PENDING="$(printf '%s' "${PIPELINE}" | json_field pending_capacity_station_count)"
        QUEUE_AGE="$(printf '%s' "${PIPELINE}" | json_field queue_age_seconds)"
        echo "  INFO  active stations: ${ACTIVE:-0}, pending capacity: ${PENDING:-0}"
        echo "  INFO  queue age: ${QUEUE_AGE:-none}s"

        # A queue that keeps growing means consumers cannot keep up; that is a
        # capacity problem, not a health problem, so it warns rather than fails.
        if [ -n "${QUEUE_AGE}" ] && [ "${QUEUE_AGE}" != "None" ]; then
            if python3 -c "import sys; sys.exit(0 if float('${QUEUE_AGE}') > 900 else 1)"; then
                echo "  WARN  oldest queued work is over 15 minutes old"
            fi
        fi
    fi
else
    echo "  INFO  legacy mode: pipeline checks skipped"
fi

# --- existing API contract ---------------------------------------------------

CAMPAIGNS_CODE="$(status_of "${BASE_URL}/api/v1/campaigns")"
[ "${CAMPAIGNS_CODE}" = "200" ] && pass "/api/v1/campaigns: 200" \
    || fail "/api/v1/campaigns: HTTP ${CAMPAIGNS_CODE}"

echo
if [ "${FAILURES}" -gt 0 ]; then
    echo "smoke-test: FAILED (${FAILURES} check(s))" >&2
    exit 1
fi
echo "smoke-test: PASS"
