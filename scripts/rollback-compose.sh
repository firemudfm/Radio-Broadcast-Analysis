#!/usr/bin/env bash
# Roll back to a previously deployed release.
#
#   scripts/rollback-compose.sh --previous [--dry-run]
#   scripts/rollback-compose.sh --to-commit <40-hex-sha> [--dry-run]
#
# Rolls back CODE AND IMAGES ONLY.
#
# The database is deliberately NOT restored. Reverting a SQLite file would
# discard every mention, transcript and analysis written since the backup, and
# the schema is forward-only by policy (ADR-004). If a schema change genuinely
# has to be undone, that is a separate, explicit, operator-driven restore from
# /var/lib/radio/backups -- never a side effect of rolling back code.
#
# A backup IS taken before the rollback, so the current state is recoverable if
# the older code turns out to be incompatible with the newer schema.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

TARGET_COMMIT=""
USE_PREVIOUS=0
DRY_RUN=0
COMPOSE_ENV="${RADIO_COMPOSE_ENV:-/etc/radio-broadcast-analysis/compose.env}"
LOCK_FILE="${RADIO_DEPLOY_LOCK:-/var/lock/radio-compose-deploy.lock}"
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"

usage() {
    cat <<'USAGE'
Roll back to a previously deployed release (code and images only).

Usage:
  scripts/rollback-compose.sh --previous            [--dry-run]
  scripts/rollback-compose.sh --to-commit <sha>     [--dry-run]

Options:
  --previous          Roll back to the release recorded as previous.
  --to-commit SHA     Roll back to an explicit full 40-character commit.
                      Branch names are refused.
  --dry-run           Validate everything, change no container.
  --compose-env PATH  Compose CLI env file.
  -h, --help          Show this help.

The database is NOT restored. A backup is taken first; restoring it is a
separate explicit operator action.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --previous)    USE_PREVIOUS=1; shift ;;
        --to-commit)   TARGET_COMMIT="${2:-}"; shift 2 ;;
        --compose-env) COMPOSE_ENV="${2:-}"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit "${EXIT_OK}" ;;
        *)             usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

if [ "${USE_PREVIOUS}" -eq 0 ] && [ -z "${TARGET_COMMIT}" ]; then
    usage >&2
    die "${EXIT_USAGE}" "one of --previous or --to-commit is required"
fi
if [ "${USE_PREVIOUS}" -eq 1 ] && [ -n "${TARGET_COMMIT}" ]; then
    die "${EXIT_USAGE}" "--previous and --to-commit are mutually exclusive"
fi

stage "1/9  Validating tooling"
require_commands git docker stat python3
[ "${DRY_RUN}" -eq 1 ] || require_commands flock

stage "2/9  Loading Compose environment"
require_env_file "${COMPOSE_ENV}"
set -a
# shellcheck disable=SC1090
source "${COMPOSE_ENV}"
set +a
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-radio-prod}"
RELEASE_ROOT="${RADIO_RELEASE_ROOT:-${DATA_ROOT}/releases}"
DEPLOY_ROOT="${RADIO_DEPLOY_ROOT:-${DATA_ROOT}/deploy}"
STATE_FILE="${DEPLOY_ROOT}/state.json"
export RADIO_API_PUBLISH_HOST="${RADIO_API_PUBLISH_HOST:-127.0.0.1}"
export RADIO_CONTAINER_UID="${RADIO_CONTAINER_UID:-10001}"
export RADIO_CONTAINER_GID="${RADIO_CONTAINER_GID:-10001}"

stage "3/9  Resolving the rollback target"
if [ "${USE_PREVIOUS}" -eq 1 ]; then
    TARGET_COMMIT="$(read_state_field "${STATE_FILE}" previous_commit)"
    [ -n "${TARGET_COMMIT}" ] || TARGET_COMMIT="$(read_release_target "${RELEASE_ROOT}/previous")"
    [ -n "${TARGET_COMMIT}" ] || die "${EXIT_PRECONDITION}" "no previous release is recorded"
fi
validate_full_sha "${TARGET_COMMIT}"

CURRENT_COMMIT="$(read_state_field "${STATE_FILE}" current_commit)"
if [ "${TARGET_COMMIT}" = "${CURRENT_COMMIT}" ]; then
    die "${EXIT_USAGE}" "release ${TARGET_COMMIT} is already current"
fi
log "rolling back from ${CURRENT_COMMIT:-unknown} to ${TARGET_COMMIT}"

stage "4/9  Validating the target release"
TARGET_DIR="${RELEASE_ROOT}/${TARGET_COMMIT}"
[ -d "${TARGET_DIR}" ] || die "${EXIT_PRECONDITION}" "release directory ${TARGET_DIR} does not exist"
[ -f "${TARGET_DIR}/.release-manifest.json" ] \
    || die "${EXIT_PRECONDITION}" "release ${TARGET_COMMIT} has no manifest; refusing to roll back to an unverified directory"
for required in compose.yaml compose.prod.yaml scripts/smoke-test.sh; do
    [ -e "${TARGET_DIR}/${required}" ] \
        || die "${EXIT_PRECONDITION}" "release ${TARGET_COMMIT} is missing ${required}"
done
log "release directory and manifest valid"

stage "5/9  Validating target images"
export RADIO_API_IMAGE="radio-api:${TARGET_COMMIT}"
export RADIO_PIPELINE_IMAGE="radio-pipeline:${TARGET_COMMIT}"
export RADIO_LLM_IMAGE="radio-llm:${TARGET_COMMIT}"
docker image inspect "${RADIO_API_IMAGE}" >/dev/null 2>&1 \
    || die "${EXIT_PRECONDITION}" \
       "image ${RADIO_API_IMAGE} is not present locally; rollback never rebuilds, so the original image must still exist"
log "api image present"

COMPOSE_FILES=(-f "${TARGET_DIR}/compose.yaml" -f "${TARGET_DIR}/compose.prod.yaml")
compose() { docker compose --project-name "${COMPOSE_PROJECT_NAME}" "${COMPOSE_FILES[@]}" "$@"; }

STAGE_NAME="$(read_state_field "${STATE_FILE}" stage)"
STAGE_NAME="${STAGE_NAME:-api}"
case "${STAGE_NAME}" in
    api)  PROFILES=(--profile core); SERVICES=(api) ;;
    core) PROFILES=(--profile core); SERVICES=(api planner) ;;
    full) PROFILES=(--profile core --profile pipeline --profile llm)
          SERVICES=(api planner listener transcription-worker analysis-worker cleanup-worker llm) ;;
    *)    PROFILES=(--profile core); SERVICES=(api) ;;
esac
log "target stage ${STAGE_NAME}"

stage "6/9  Validating the rendered Compose configuration"
compose "${PROFILES[@]}" config >/dev/null \
    || die "${EXIT_PRECONDITION}" "compose config failed for release ${TARGET_COMMIT}"
log "compose configuration valid"

if [ "${DRY_RUN}" -eq 1 ]; then
    stage "Dry run complete"
    log "every gate passed; no container was changed and no backup was taken"
    log "would start: ${SERVICES[*]}"
    exit "${EXIT_OK}"
fi

stage "7/9  Acquiring the deployment lock"
mkdir -p "$(dirname "${LOCK_FILE}")" 2>/dev/null || true
acquire_deploy_lock "${LOCK_FILE}"

stage "8/9  Backing up the database before rollback"
warn "the database will NOT be restored; only code and images roll back"
DATABASE_FILE="${DATA_ROOT}/database/radio.db"
BACKUP_PATH=""
if [ -f "${DATABASE_FILE}" ]; then
    RADIO_DATABASE_PATH="${DATABASE_FILE}" RADIO_HOST_BACKUPS="${DATA_ROOT}/backups" \
        bash "${TARGET_DIR}/scripts/backup-sqlite.sh" \
        || die "${EXIT_ROLLBACK}" "pre-rollback database backup failed"
    log "backup complete; restoring it is a separate explicit action"
else
    log "no database present; nothing to back up"
fi

stage "9/9  Starting the target release"
compose "${PROFILES[@]}" up -d --remove-orphans "${SERVICES[@]}" \
    || die "${EXIT_ROLLBACK}" "compose up failed during rollback"

log "waiting for container health"
if ! wait_for_health "${EXIT_ROLLBACK}" 300 "${SERVICES[@]}"; then
    compose "${PROFILES[@]}" logs --tail=80 || true
    die "${EXIT_ROLLBACK}" "rolled-back containers did not become healthy within 300s"
fi

bash "${TARGET_DIR}/scripts/smoke-test.sh" "http://127.0.0.1:8788" \
    || die "${EXIT_ROLLBACK}" "smoke test failed after rollback"

# Symlinks and state move only now, after the target proved healthy.
[ -n "${CURRENT_COMMIT}" ] && point_symlink_atomic "${RELEASE_ROOT}/previous" "${RELEASE_ROOT}/${CURRENT_COMMIT}"
point_symlink_atomic "${RELEASE_ROOT}/current" "${TARGET_DIR}"

write_state_atomic "${DEPLOY_ROOT}/state.json" "$(cat <<EOF
{
  "schema_version": 1,
  "current_commit": "${TARGET_COMMIT}",
  "previous_commit": "${CURRENT_COMMIT}",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "deployed_by": "$(id -un)",
  "stage": "${STAGE_NAME}",
  "compose_project": "${COMPOSE_PROJECT_NAME}",
  "release_path": "${TARGET_DIR}",
  "api_image": "${RADIO_API_IMAGE}",
  "pipeline_image": "${RADIO_PIPELINE_IMAGE}",
  "llm_image": "${RADIO_LLM_IMAGE}",
  "migration": "not-run (rollback)",
  "backup_path": "${BACKUP_PATH}",
  "smoke_test": "pass",
  "rolled_back": true
}
EOF
)"

stage "Rollback complete"
log "release ${TARGET_COMMIT} is live"
warn "database schema and data were NOT rolled back"
exit "${EXIT_OK}"
