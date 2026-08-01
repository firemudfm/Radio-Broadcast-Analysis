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
TARGET_STAGE=""
USE_PREVIOUS=0
DRY_RUN=0
COMPOSE_ENV="${RADIO_COMPOSE_ENV:-/etc/radio-broadcast-analysis/compose.env}"
LOCK_FILE="${RADIO_DEPLOY_LOCK:-/var/lock/radio-compose-deploy.lock}"
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"

usage() {
    cat <<'USAGE'
Roll back to a previously deployed release (code and images only).

Usage:
  scripts/rollback-compose.sh --previous                          [--dry-run]
  scripts/rollback-compose.sh --to-commit <sha> --stage <stage>   [--dry-run]

Options:
  --previous          Roll back to the release recorded as previous, using its
                      recorded commit AND stage.
  --to-commit SHA     Roll back to an explicit full 40-character commit.
                      Branch names are refused.
  --stage STAGE       api | core | full. REQUIRED with --to-commit: one commit
                      may have been released at more than one stage, and
                      guessing which -- or defaulting to api, or picking the
                      newest directory -- would start the wrong service set
                      during an incident.
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
        --stage)       TARGET_STAGE="${2:-}"; shift 2 ;;
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
# A commit may have been released at api, core and full. Which one to start is
# not something a rollback may infer -- not from the current deployment, not
# from the newest directory, and certainly not by defaulting to api.
if [ -n "${TARGET_COMMIT}" ] && [ -z "${TARGET_STAGE}" ]; then
    usage >&2
    die "${EXIT_USAGE}" \
        "--to-commit requires --stage api|core|full; a commit may exist at several stages and rollback never guesses which"
fi
if [ "${USE_PREVIOUS}" -eq 1 ] && [ -n "${TARGET_STAGE}" ]; then
    die "${EXIT_USAGE}" "--stage cannot be combined with --previous; the previous release records its own stage"
fi
# Argument validation happens here, before any host gate. A typo in --stage
# should be a usage error, not something an operator discovers only after the
# script has started complaining about environment-file permissions.
if [ -n "${TARGET_STAGE}" ]; then
    case "${TARGET_STAGE}" in
        api|core|full) ;;
        *) die "${EXIT_USAGE}" "--stage must be api, core or full (got '${TARGET_STAGE}')" ;;
    esac
fi
if [ -n "${TARGET_COMMIT}" ]; then
    validate_full_sha "${TARGET_COMMIT}"
fi

ENV_DIR="${RADIO_ENV_DIR:-/etc/radio-broadcast-analysis}"
SKIP_MOUNT_CHECK="${RADIO_SKIP_MOUNT_CHECK:-0}"
DATA_FREE_MIB="${RADIO_MIN_DATA_FREE_MIB:-1024}"

stage "1/11 Validating tooling"
require_commands git docker stat df awk python3
[ "${DRY_RUN}" -eq 1 ] || require_commands flock

stage "2/11 Loading Compose environment"
require_env_file "${COMPOSE_ENV}"
set -a
# shellcheck disable=SC1090
source "${COMPOSE_ENV}"
set +a
log "loaded ${COMPOSE_ENV} (contents not printed)"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-radio-prod}"
RELEASE_ROOT="${RADIO_RELEASE_ROOT:-${DATA_ROOT}/releases}"
DEPLOY_ROOT="${RADIO_DEPLOY_ROOT:-${DATA_ROOT}/deploy}"
STATE_FILE="${DEPLOY_ROOT}/state.json"

# ---------------------------------------------------------------------------
# A rollback changes what is running on the host exactly as much as a deploy
# does. Every non-secret host control the deploy enforces is enforced here too;
# otherwise rollback is the documented way around all of them, and it is the
# path taken under time pressure when nobody is reading carefully.
stage "3/11 Resolving container runtime identity"
HOST_IDENTITY="$(resolve_host_identity radio)"
if [ -n "${HOST_IDENTITY}" ]; then
    read -r HOST_UID HOST_GID <<<"${HOST_IDENTITY}"
    log "host 'radio' account is ${HOST_UID}:${HOST_GID}"
    # Blank means auto-detect, NOT 10001. Defaulting a blank to the development
    # uid on a host whose radio account is 992 produces containers that cannot
    # write their own bind mounts.
    RADIO_CONTAINER_UID="${RADIO_CONTAINER_UID:-${HOST_UID}}"
    RADIO_CONTAINER_GID="${RADIO_CONTAINER_GID:-${HOST_GID}}"
    if [ "${RADIO_CONTAINER_UID}" != "${HOST_UID}" ] || [ "${RADIO_CONTAINER_GID}" != "${HOST_GID}" ]; then
        warn "compose.env pins ${RADIO_CONTAINER_UID}:${RADIO_CONTAINER_GID} but the host radio account is ${HOST_UID}:${HOST_GID}"
    fi
else
    RADIO_CONTAINER_UID="${RADIO_CONTAINER_UID:-10001}"
    RADIO_CONTAINER_GID="${RADIO_CONTAINER_GID:-10001}"
    warn "no host 'radio' account found; using ${RADIO_CONTAINER_UID}:${RADIO_CONTAINER_GID} from configuration"
fi
validate_uid_gid "${RADIO_CONTAINER_UID}" "${RADIO_CONTAINER_GID}"
export RADIO_CONTAINER_UID RADIO_CONTAINER_GID

stage "4/11 Validating host state, environment and exposure"
if [ "${SKIP_MOUNT_CHECK}" != "1" ]; then
    require_mountpoint "${DATA_ROOT}"
else
    warn "mount check skipped (RADIO_SKIP_MOUNT_CHECK=1); intended for non-production validation only"
fi
require_writable_ownership "${RADIO_CONTAINER_UID}" "${RADIO_CONTAINER_GID}" \
    "${DATA_ROOT}/database" "${DATA_ROOT}/spool" "${DATA_ROOT}/evidence" \
    "${DATA_ROOT}/logs" "${DATA_ROOT}/backups"
require_env_file "${ENV_DIR}/infrastructure.env"
require_env_file "${ENV_DIR}/application.env"
reject_placeholder_secret "${ENV_DIR}/application.env"
reject_static_aws_credentials "${ENV_DIR}/infrastructure.env" "${ENV_DIR}/application.env"
log "environment files present with safe permissions (contents not printed)"

export RADIO_API_PUBLISH_HOST="${RADIO_API_PUBLISH_HOST:-127.0.0.1}"
RADIO_ALLOW_DIRECT_HTTP="${RADIO_ALLOW_DIRECT_HTTP:-0}"
validate_publish_host "${RADIO_API_PUBLISH_HOST}" "${RADIO_ALLOW_DIRECT_HTTP}"
log "API publish host: ${RADIO_API_PUBLISH_HOST}"

# A rollback takes a backup before it starts, so it needs room for one.
require_free_space "${DATA_ROOT}" "${DATA_FREE_MIB}"

stage "5/11 Resolving the rollback target"
# The target identity is COMMIT + STAGE. --previous reads both from state, and
# falls back to the previous symlink's validated identity -- not its basename,
# which is now merely the stage.
if [ "${USE_PREVIOUS}" -eq 1 ]; then
    TARGET_COMMIT="$(read_state_field "${STATE_FILE}" previous_commit)"
    TARGET_STAGE="$(read_state_field "${STATE_FILE}" previous_stage)"
    if [ -z "${TARGET_COMMIT}" ] || [ -z "${TARGET_STAGE}" ]; then
        PREVIOUS_IDENTITY="$(read_release_identity "${RELEASE_ROOT}/previous" "${RELEASE_ROOT}" 2>/dev/null || true)"
        if [ -n "${PREVIOUS_IDENTITY}" ]; then
            TARGET_COMMIT="$(release_identity_commit "${PREVIOUS_IDENTITY}")"
            TARGET_STAGE="$(release_identity_stage "${PREVIOUS_IDENTITY}")"
        fi
    fi
    [ -n "${TARGET_COMMIT}" ] && [ -n "${TARGET_STAGE}" ] \
        || die "${EXIT_PRECONDITION}" "no complete previous release identity (commit and stage) is recorded"
fi
validate_full_sha "${TARGET_COMMIT}"
case "${TARGET_STAGE}" in
    api|core|full) ;;
    *) die "${EXIT_USAGE}" "--stage must be api, core or full (got '${TARGET_STAGE}')" ;;
esac

CURRENT_COMMIT="$(read_state_field "${STATE_FILE}" current_commit)"
CURRENT_STAGE="$(read_state_field "${STATE_FILE}" current_stage)"
[ -n "${CURRENT_STAGE}" ] || CURRENT_STAGE="$(read_state_field "${STATE_FILE}" stage)"
# Identity is commit AND stage, so X/core -> X/api is a legitimate rollback even
# though the commit is unchanged.
if [ "${TARGET_COMMIT}" = "${CURRENT_COMMIT}" ] && [ "${TARGET_STAGE}" = "${CURRENT_STAGE}" ]; then
    die "${EXIT_USAGE}" "release ${TARGET_COMMIT} at stage ${TARGET_STAGE} is already current"
fi
log "rolling back from ${CURRENT_COMMIT:-unknown}/${CURRENT_STAGE:-unknown} to ${TARGET_COMMIT}/${TARGET_STAGE}"

stage "6/11 Validating the target release manifest"
TARGET_DIR="$(release_path "${RELEASE_ROOT}" "${TARGET_COMMIT}" "${TARGET_STAGE}")"
# The TARGET release's own manifest is validated against the full requested
# identity. Its stage is not inferred from state.json, which describes the
# release being rolled AWAY from: rolling a full deployment back to an api
# release must start an api service set, and starting workers against code that
# never shipped with them is exactly the failure this prevents.
validate_release_manifest "${TARGET_DIR}" "${TARGET_COMMIT}" "${TARGET_STAGE}" >/dev/null \
    || die "${EXIT_PRECONDITION}" \
       "release ${TARGET_COMMIT} at stage ${TARGET_STAGE} did not pass manifest validation"
STAGE_NAME="${TARGET_STAGE}"
log "target manifest valid: ${TARGET_COMMIT} at stage ${STAGE_NAME}"

stage "7/11 Validating target images"
export RADIO_API_IMAGE="radio-api:${TARGET_COMMIT}"
export RADIO_PIPELINE_IMAGE="radio-pipeline:${TARGET_COMMIT}"
export RADIO_LLM_IMAGE="radio-llm:${TARGET_COMMIT}"
# Every image the TARGET stage needs, checked before a single container is
# touched. Rollback never builds and never pulls, so a missing image cannot be
# repaired here; finding out half-way through leaves the host running neither
# release.
require_stage_images "${STAGE_NAME}" "${TARGET_COMMIT}" \
    || die "${EXIT_PRECONDITION}" \
       "rollback never rebuilds or pulls, so every image for stage ${STAGE_NAME} must already exist"

COMPOSE_FILES=(-f "${TARGET_DIR}/compose.yaml" -f "${TARGET_DIR}/compose.prod.yaml")
compose() { docker compose --project-name "${COMPOSE_PROJECT_NAME}" "${COMPOSE_FILES[@]}" "$@"; }

read -r -a PROFILES <<<"$(stage_profile_args "${STAGE_NAME}")"
read -r -a SERVICES <<<"$(stage_plan "${STAGE_NAME}" runtime_services)"
log "target stage ${STAGE_NAME}: [${SERVICES[*]}]"

stage "8/11 Validating the rendered Compose configuration"
compose "${PROFILES[@]}" config >/dev/null \
    || die "${EXIT_PRECONDITION}" "compose config failed for release ${TARGET_COMMIT}"
log "compose configuration valid"

if [ "${DRY_RUN}" -eq 1 ]; then
    stage "Dry run complete"
    log "every gate passed; no container was changed and no backup was taken"
    log "would start: ${SERVICES[*]}"
    exit "${EXIT_OK}"
fi

stage "9/11 Acquiring the deployment lock"
mkdir -p "$(dirname "${LOCK_FILE}")" 2>/dev/null || true
acquire_deploy_lock "${LOCK_FILE}"

stage "10/11 Backing up the database before rollback"
warn "the database will NOT be restored; only code and images roll back"
DATABASE_FILE="${DATA_ROOT}/database/radio.db"
BACKUP_PATH=""
if [ -f "${DATABASE_FILE}" ]; then
    BACKUP_OUTPUT="$(mktemp)"
    if ! RADIO_DATABASE_PATH="${DATABASE_FILE}" RADIO_HOST_BACKUPS="${DATA_ROOT}/backups" \
         bash "${TARGET_DIR}/scripts/backup-sqlite.sh" >"${BACKUP_OUTPUT}" 2>&1; then
        cat "${BACKUP_OUTPUT}" >&2 || true
        rm -f "${BACKUP_OUTPUT}"
        die "${EXIT_ROLLBACK}" "pre-rollback database backup failed"
    fi
    cat "${BACKUP_OUTPUT}"
    BACKUP_PATH="$(parse_backup_path "${BACKUP_OUTPUT}")" || {
        rm -f "${BACKUP_OUTPUT}"
        die "${EXIT_ROLLBACK}" "backup completed but did not report a usable path"
    }
    rm -f "${BACKUP_OUTPUT}"
    log "backup complete at ${BACKUP_PATH}; restoring it is a separate explicit action"
else
    log "no database present; nothing to back up"
fi

stage "11/11 Starting the target release"
# --no-build --pull never: every image was verified above. Compose would
# otherwise build a missing tag from the release directory, which is how a
# rollback quietly produces a brand-new, unreviewed image instead of restoring
# the artifact that was actually running before.
compose "${PROFILES[@]}" up -d --no-build --pull never --remove-orphans "${SERVICES[@]}" \
    || die "${EXIT_ROLLBACK}" "compose up failed during rollback"

log "waiting for container health"
if ! wait_for_health "${EXIT_ROLLBACK}" 300 "${SERVICES[@]}"; then
    compose "${PROFILES[@]}" logs --tail=80 || true
    die "${EXIT_ROLLBACK}" "rolled-back containers did not become healthy within 300s"
fi

# Narrowing the stage (full -> api) leaves the excluded services running:
# --remove-orphans does not remove a service that is still defined but belongs
# to a profile this stage does not activate.
reconcile_stage_services "${STAGE_NAME}" \
    || die "${EXIT_ROLLBACK}" "could not reconcile to the exact ${STAGE_NAME} service set"

bash "${TARGET_DIR}/scripts/smoke-test.sh" --stage "${STAGE_NAME}" "http://127.0.0.1:8788" \
    || die "${EXIT_ROLLBACK}" "smoke test failed after rollback"

# Symlinks and state move only now, after the target proved healthy.
if [ -n "${CURRENT_COMMIT}" ] && [ -n "${CURRENT_STAGE}" ]; then
    point_symlink_atomic "${RELEASE_ROOT}/previous" \
        "$(release_path "${RELEASE_ROOT}" "${CURRENT_COMMIT}" "${CURRENT_STAGE}")"
fi
point_symlink_atomic "${RELEASE_ROOT}/current" "${TARGET_DIR}"

write_state_atomic "${DEPLOY_ROOT}/state.json" "$(cat <<EOF
{
  "schema_version": 1,
  "current_commit": "${TARGET_COMMIT}",
  "current_stage": "${STAGE_NAME}",
  "previous_commit": "${CURRENT_COMMIT}",
  "previous_stage": "${CURRENT_STAGE}",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "deployed_by": "$(id -un)",
  "stage": "${STAGE_NAME}",
  "compose_project": "${COMPOSE_PROJECT_NAME}",
  "release_path": "${TARGET_DIR}",
  "runtime_services": "$(stage_plan "${STAGE_NAME}" runtime_services)",
  "api_image": $(json_image_field "${STAGE_NAME}" "${TARGET_COMMIT}" radio-api tag),
  "api_image_id": $(json_image_field "${STAGE_NAME}" "${TARGET_COMMIT}" radio-api id),
  "pipeline_image": $(json_image_field "${STAGE_NAME}" "${TARGET_COMMIT}" radio-pipeline tag),
  "pipeline_image_id": $(json_image_field "${STAGE_NAME}" "${TARGET_COMMIT}" radio-pipeline id),
  "llm_image": $(json_image_field "${STAGE_NAME}" "${TARGET_COMMIT}" radio-llm tag),
  "llm_image_id": $(json_image_field "${STAGE_NAME}" "${TARGET_COMMIT}" radio-llm id),
  "migration": "not-run (rollback)",
  "backup_created": $([ -n "${BACKUP_PATH}" ] && printf true || printf false),
  "backup_path": "${BACKUP_PATH}",
  "database_restored": false,
  "smoke_test": "pass",
  "rolled_back": true
}
EOF
)"

stage "Rollback complete"
log "release ${TARGET_COMMIT} is live"
warn "database schema and data were NOT rolled back"
exit "${EXIT_OK}"
