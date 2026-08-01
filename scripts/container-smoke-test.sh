#!/usr/bin/env bash
# Build and exercise the API container in isolation.
#
#   scripts/container-smoke-test.sh [--port N] [--keep]
#
# A LOCAL/CI test, not a deployment. It proves the API image actually builds and
# serves, which no unit test can: the Dockerfile, the entrypoint, the non-root
# user, the healthcheck and the bind-mount permissions are only exercised here.
#
# Isolation is deliberate and total:
#   * a unique Compose project name, so an existing local stack is untouched;
#   * temporary directories and temporary env files, removed on exit;
#   * a generated throwaway token secret -- no real secret is ever read;
#   * an obviously fake S3 bucket;
#   * RADIO_PIPELINE_MODE=legacy, so no SQS queue is required;
#   * a non-production port;
#   * no worker, no model, no AWS call.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PORT="${RADIO_SMOKE_PORT:-18788}"
KEEP=0
PROJECT="radio-smoke-$$"
WORKDIR=""

usage() {
    cat <<'USAGE'
Build and smoke-test the API container in isolation.

Usage:
  scripts/container-smoke-test.sh [--port N] [--keep]

Options:
  --port N   Loopback port to bind (default 18788).
  --keep     Leave the container running for inspection.
  -h, --help Show this help.

Builds only the API image. Starts no worker, downloads no model, contacts no
AWS endpoint.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)    PORT="${2:-}"; shift 2 ;;
        --keep)    KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)         usage >&2; printf 'unknown argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 65; }

# Docker daemons on Windows do not understand MSYS/Git-Bash paths such as
# /c/Users/... or /tmp/... . cygpath -m converts to a forward-slash Windows path
# the daemon accepts. On Linux and macOS this is a pass-through.
hostpath() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        printf '%s' "$1"
    fi
}

cleanup() {
    local code=$?
    if [ "${code}" -ne 0 ] && [ -n "${WORKDIR}" ]; then
        printf '\n--- container logs (failure) ---\n' >&2
        docker compose -p "${PROJECT}" -f "${WORKDIR}/compose.smoke.yaml" logs --tail=120 >&2 2>&1 || true
    fi
    if [ "${KEEP}" -eq 0 ]; then
        printf '\n==> Cleaning up\n'
        if [ -n "${WORKDIR}" ] && [ -f "${WORKDIR}/compose.smoke.yaml" ]; then
            # No -v: this stack declares no named volume, and removing volumes
            # by default is how someone's local data disappears.
            docker compose -p "${PROJECT}" -f "${WORKDIR}/compose.smoke.yaml" \
                down --remove-orphans >/dev/null 2>&1 || true
        fi
        [ -n "${WORKDIR}" ] && rm -rf "${WORKDIR}"
    else
        printf '\n==> --keep: project %s left running (workdir %s)\n' "${PROJECT}" "${WORKDIR}"
    fi
    exit "${code}"
}
trap cleanup EXIT

printf '==> Preparing an isolated workspace\n'
WORKDIR="$(mktemp -d)"
mkdir -p "${WORKDIR}/database" "${WORKDIR}/logs" "${WORKDIR}/evidence" "${WORKDIR}/spool"

# Generated per run and thrown away. Never reads a real secret.
SMOKE_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

cat > "${WORKDIR}/smoke.env" <<EOF
AWS_REGION=eu-north-1
RADIO_S3_BUCKET=smoke-test-bucket-does-not-exist
RADIO_AUDIO_TOKEN_SECRET=${SMOKE_SECRET}
RADIO_PIPELINE_MODE=legacy
RADIO_SYNC_ENABLED=false
RADIO_SYNC_ON_STARTUP=false
RADIO_LLM_ENABLED=false
RADIO_ANALYSIS_WORKER_ENABLED=false
RADIO_DATABASE_PATH=/var/lib/radio/database/radio.db
RADIO_LOG_PATH=/var/lib/radio/logs
LOG_LEVEL=INFO
EOF
chmod 0600 "${WORKDIR}/smoke.env"

# A minimal standalone Compose file: the production one carries profiles,
# resource limits and bind mounts that have no meaning here.
cat > "${WORKDIR}/compose.smoke.yaml" <<EOF
services:
  api:
    image: radio-api:smoke-${PROJECT}
    build:
      context: $(hostpath "${REPO_ROOT}")
      dockerfile: docker/api.Dockerfile
      args:
        RADIO_UID: "\${RADIO_CONTAINER_UID:-10001}"
        RADIO_GID: "\${RADIO_CONTAINER_GID:-10001}"
    init: true
    read_only: true
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    tmpfs: ["/tmp:rw,noexec,nosuid,size=32m"]
    env_file: ["$(hostpath "${WORKDIR}")/smoke.env"]
    ports: ["127.0.0.1:${PORT}:8788"]
    volumes:
      - "$(hostpath "${WORKDIR}")/database:/var/lib/radio/database:rw"
      - "$(hostpath "${WORKDIR}")/logs:/var/lib/radio/logs:rw"
    healthcheck:
      test: ["CMD", "python", "/app/healthchecks/api.py"]
      interval: 5s
      timeout: 5s
      start_period: 20s
      retries: 12
    environment:
      RADIO_API_PORT: "8788"
EOF

printf '==> Building the API image only\n'
docker compose -p "${PROJECT}" -f "${WORKDIR}/compose.smoke.yaml" build api

printf '==> Starting the API container\n'
docker compose -p "${PROJECT}" -f "${WORKDIR}/compose.smoke.yaml" up -d api

printf '==> Waiting for Docker health\n'
deadline=$(( $(date +%s) + 180 ))
while :; do
    cid="$(docker compose -p "${PROJECT}" -f "${WORKDIR}/compose.smoke.yaml" ps -q api)"
    [ -n "${cid}" ] || { echo "container did not start" >&2; exit 72; }
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}")"
    [ "${health}" = "healthy" ] && break
    if [ "${health}" = "unhealthy" ] || [ "$(date +%s)" -ge "${deadline}" ]; then
        echo "API container did not become healthy (status: ${health})" >&2
        exit 72
    fi
    sleep 3
done
printf '    healthy\n'

BASE="http://127.0.0.1:${PORT}"
fail=0
check() {
    local label="$1" url="$2"
    if curl -fsS --max-time 10 "${url}" >/dev/null 2>&1; then
        printf '    PASS  %s\n' "${label}"
    else
        printf '    FAIL  %s (%s)\n' "${label}" "${url}" >&2
        fail=1
    fi
}

printf '==> Verifying the API contract\n'
check "/healthz" "${BASE}/healthz"
check "/readyz"  "${BASE}/readyz"

# The published route table, not a data query. Every campaign and mention
# endpoint reads from S3, and this container is deliberately pointed at a bucket
# that does not exist with no credentials attached -- so asserting one of them
# returns rows would really only assert that the smoke test had been handed AWS
# access, which is exactly what it must never need. /openapi.json is rendered
# from the assembled application, so it proves both routers mounted and that the
# contract the frontend depends on is still published.
#
# The expected routes are inlined into the Python program rather than passed in
# as an argument or an environment value. MSYS/Git-Bash rewrites anything that
# starts with `/` into a Windows path when it calls a native binary, which
# silently turned /api/v1/... into C:/code/Git/api/v1/... and failed the check
# for entirely the wrong reason. A `-c` program does not look like a path, so it
# crosses that boundary untouched.
if openapi="$(curl -fsS --max-time 10 "${BASE}/openapi.json" 2>/dev/null)"; then
    missing="$(printf '%s' "${openapi}" | python3 -c '
import json, sys
expected = [
    "/api/v1/brand-signal/campaigns",
    "/api/v1/brand-signal/stations",
    "/api/v1/brand-signal/mentions",
    "/api/v1/radio-catalog/stations",
]
paths = set(json.load(sys.stdin).get("paths") or {})
print(" ".join(p for p in expected if p not in paths))
' 2>/dev/null || echo 'openapi-unparseable')"
    if [ -z "${missing}" ]; then
        printf '    PASS  /openapi.json publishes every expected route\n'
    else
        printf '    FAIL  /openapi.json is missing: %s\n' "${missing}" >&2
        fail=1
    fi
else
    printf '    FAIL  /openapi.json (%s)\n' "${BASE}/openapi.json" >&2
    fail=1
fi

# auth_mode must stay `none` for the pilot; a change here breaks the frontend.
mode="$(curl -fsS --max-time 10 "${BASE}/healthz" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("auth_mode",""))' 2>/dev/null || echo '')"
if [ "${mode}" = "none" ]; then
    printf '    PASS  auth_mode is none\n'
else
    printf '    FAIL  auth_mode is %s\n' "${mode:-unknown}" >&2
    fail=1
fi

# The container must run non-root.
runtime_uid="$(docker exec "${cid}" id -u 2>/dev/null || echo '')"
if [ -n "${runtime_uid}" ] && [ "${runtime_uid}" != "0" ]; then
    printf '    PASS  container runs as uid %s (non-root)\n' "${runtime_uid}"
else
    printf '    FAIL  container runs as uid %s\n' "${runtime_uid:-unknown}" >&2
    fail=1
fi

printf '\n'
if [ "${fail}" -ne 0 ]; then
    echo "container-smoke-test: FAILED" >&2
    exit 73
fi
echo "container-smoke-test: PASS"
exit 0
