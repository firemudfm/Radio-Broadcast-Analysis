#!/usr/bin/env bash
# Reclaim spool space through the existing cleanup service.
#
#   scripts/cleanup-spool.sh --image radio-pipeline:<sha> [--dry-run]
#
# This is a THIN WRAPPER on purpose. It does not contain a `find -delete`, an
# age threshold, or any deletion policy of its own.
#
# The policy lives in app/workers/cleanup.py, which joins every candidate
# against SQLite job state: a segment that is still `pending` transcription, or
# that a confirmed mention depends on, is never deleted -- at any watermark.
# A shell re-implementation would inevitably drift from that and delete audio
# that was about to become a mention. Deleting audio is the one irreversible
# thing this system does, so there is exactly one implementation of "safe".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

IMAGE=""
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
ENV_DIR="${RADIO_ENV_DIR:-/etc/radio-broadcast-analysis}"
RUN_UID=""
RUN_GID=""
DRY_RUN=0

usage() {
    cat <<'USAGE'
Run one safe spool-cleanup cycle through the existing cleanup service.

Usage:
  scripts/cleanup-spool.sh --image radio-pipeline:<sha> [options]

Required:
  --image TAG        Pipeline image to run.

Options:
  --dry-run          Report what would be reclaimed; delete nothing.
  --data-root PATH   Host data root (default /var/lib/radio).
  --env-dir PATH     Environment file directory.
  --uid N / --gid N  Container runtime identity.
  -h, --help         Show this help.

Retention, watermarks and containment are enforced by the cleanup service, not
by this script. In-flight segments and audio belonging to a confirmed mention
are never deleted.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)     IMAGE="${2:-}"; shift 2 ;;
        --data-root) DATA_ROOT="${2:-}"; shift 2 ;;
        --env-dir)   ENV_DIR="${2:-}"; shift 2 ;;
        --uid)       RUN_UID="${2:-}"; shift 2 ;;
        --gid)       RUN_GID="${2:-}"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   usage; exit "${EXIT_OK}" ;;
        *)           usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

[ -n "${IMAGE}" ] || { usage >&2; die "${EXIT_USAGE}" "--image is required"; }

require_commands docker stat

stage "Validating inputs"
docker image inspect "${IMAGE}" >/dev/null 2>&1 \
    || die "${EXIT_PRECONDITION}" "image ${IMAGE} is not present locally"

if [ -z "${RUN_UID}" ] || [ -z "${RUN_GID}" ]; then
    identity="$(resolve_host_identity radio)"
    if [ -n "${identity}" ]; then
        read -r RUN_UID RUN_GID <<<"${identity}"
    else
        RUN_UID="${RUN_UID:-10001}"
        RUN_GID="${RUN_GID:-10001}"
    fi
fi
validate_uid_gid "${RUN_UID}" "${RUN_GID}"

for required in database spool logs; do
    [ -d "${DATA_ROOT}/${required}" ] \
        || die "${EXIT_PRECONDITION}" "${DATA_ROOT}/${required} does not exist"
done
require_env_file "${ENV_DIR}/infrastructure.env"
require_env_file "${ENV_DIR}/application.env"

stage "Running one cleanup cycle$([ "${DRY_RUN}" -eq 1 ] && printf ' (dry run)')"
ARGS=(-m app.workers.cleanup --once)
[ "${DRY_RUN}" -eq 1 ] && ARGS+=(--dry-run)

# --network none: reclaiming local disk needs no network, and denying it proves
# this cannot reach S3 or SQS. Evidence is mounted read-only so a cleanup cycle
# physically cannot remove a retained clip.
docker run --rm \
    --name "radio-cleanup-$$" \
    --network none \
    --user "${RUN_UID}:${RUN_GID}" \
    --env-file "${ENV_DIR}/infrastructure.env" \
    --env-file "${ENV_DIR}/application.env" \
    --env "RADIO_DATABASE_PATH=/var/lib/radio/database/radio.db" \
    --env "RADIO_SPOOL_PATH=/var/lib/radio/spool" \
    --volume "${DATA_ROOT}/database:/var/lib/radio/database:rw" \
    --volume "${DATA_ROOT}/spool:/var/lib/radio/spool:rw" \
    --volume "${DATA_ROOT}/evidence:/var/lib/radio/evidence:ro" \
    --volume "${DATA_ROOT}/logs:/var/lib/radio/logs:rw" \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --entrypoint python \
    "${IMAGE}" \
    "${ARGS[@]}" \
    || die "${EXIT_PRECONDITION}" "cleanup cycle exited non-zero"

stage "Cleanup complete"
exit "${EXIT_OK}"
