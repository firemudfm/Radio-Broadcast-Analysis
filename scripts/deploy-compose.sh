#!/usr/bin/env bash
# Deploy an exact reviewed commit as an immutable Compose release.
#
#   scripts/deploy-compose.sh --commit <40-hex-sha> [--stage api|core|full]
#                             [--dry-run] [--repo PATH] [--compose-env PATH]
#
# Design, and why each rule exists:
#
#   * EXACT COMMIT ONLY. A branch name would let the deployed content change
#     between approval and execution, which defeats the point of reviewing it.
#   * NO NETWORK GIT. This script never runs pull, fetch, reset or checkout.
#     Whoever approves the commit is responsible for it being present locally.
#     That keeps the deployment step auditable and offline.
#   * IMMUTABLE RELEASES. `git archive` into /var/lib/radio/releases/<sha>.
#     Nothing is ever edited in place, so rollback is a symlink move.
#   * FAIL CLOSED, FAIL EARLY. Every gate runs before anything is built, and
#     every gate before the first container change leaves the running release
#     completely untouched.
#   * NEVER RESTORE A DATABASE AUTOMATICALLY. Code and images roll back; data
#     does not. Silently reverting a database loses everything written since
#     the backup, and that is an operator decision.
#
# What actually happens when a deployment fails:
#
#   * BEFORE any container changed: nothing is touched. The running release is
#     still running. The release directory is kept for inspection.
#   * AFTER containers changed, with NO previous release (first deployment):
#     the services this run started are stopped and removed, so a broken stack
#     is not left presenting itself as deployed. The release directory, the
#     database backup and the deploy log are preserved. No symlink is moved and
#     no deployment state is written.
#   * AFTER containers changed, WITH a previous release: the previous release
#     is restored automatically, in-process, from the existing immutable
#     release directory and the already-built images. It never rebuilds and
#     never restores SQLite. If that recovery itself fails, the exact manual
#     rollback command is printed.
#
# Either way the outcome is recorded in ${DEPLOY_ROOT}/history/failed-*.json,
# including whether recovery succeeded.
#
# This script does not touch AWS, does not use AWS-RunShellScript, and is
# intended to be invoked later by a separately reviewed fixed SSM document.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

COMMIT=""
STAGE="api"
DRY_RUN=0
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_ENV="${RADIO_COMPOSE_ENV:-/etc/radio-broadcast-analysis/compose.env}"
ENV_DIR="${RADIO_ENV_DIR:-/etc/radio-broadcast-analysis}"
LOCK_FILE="${RADIO_DEPLOY_LOCK:-/var/lock/radio-compose-deploy.lock}"
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
SKIP_MOUNT_CHECK="${RADIO_SKIP_MOUNT_CHECK:-0}"

#: Minimum free space. An image build plus a release plus a database backup on
#: a host that then fills up is a far worse outcome than refusing to start.
ROOT_FREE_MIB="${RADIO_MIN_ROOT_FREE_MIB:-3072}"
DATA_FREE_MIB="${RADIO_MIN_DATA_FREE_MIB:-2048}"

usage() {
    cat <<'USAGE'
Deploy an exact commit as an immutable Compose release.

Usage:
  scripts/deploy-compose.sh --commit <40-hex-sha> [options]

Required:
  --commit SHA         Full 40-character commit id. Branch names and short
                       shas are refused: the deployed content must be exactly
                       what was reviewed.

Options:
  --stage STAGE        api (default) | core | full
                         api  : API only. No planner, listener, ASR or LLM.
                         core : API + planner. No capture, no models.
                         full : every approved profile. Requires verified models.
  --dry-run            Run every validation gate, build and start nothing.
  --repo PATH          Source repository (default: this checkout).
  --compose-env PATH   Compose CLI env file
                       (default: /etc/radio-broadcast-analysis/compose.env).
  -h, --help           Show this help.

Exit codes:
  0 ok | 64 usage | 65 precondition | 66 locked | 70 build
  71 migration | 72 health | 73 smoke | 74 rollback
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --commit)      COMMIT="${2:-}"; shift 2 ;;
        --stage)       STAGE="${2:-}"; shift 2 ;;
        --repo)        REPO_DIR="${2:-}"; shift 2 ;;
        --compose-env) COMPOSE_ENV="${2:-}"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit "${EXIT_OK}" ;;
        *)             usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

case "${STAGE}" in
    api|core|full) ;;
    *) die "${EXIT_USAGE}" "--stage must be api, core or full (got '${STAGE}')" ;;
esac

# ---------------------------------------------------------------------------
stage "1/16  Validating tooling and arguments"
# flock is only needed when the lock is actually taken; a dry run never locks,
# so requiring it there would block validation on a host that cannot deploy.
require_commands git tar docker stat df awk python3
[ "${DRY_RUN}" -eq 1 ] || require_commands flock
validate_full_sha "${COMMIT}"
log "target commit ${COMMIT}"
log "stage ${STAGE}$([ "${DRY_RUN}" -eq 1 ] && printf ' (dry run)')"

# ---------------------------------------------------------------------------
stage "2/16  Validating the source repository"
[ -d "${REPO_DIR}/.git" ] || die "${EXIT_PRECONDITION}" "${REPO_DIR} is not a git repository"
commit_exists_locally "${REPO_DIR}" "${COMMIT}" \
    || die "${EXIT_PRECONDITION}" \
       "commit ${COMMIT} is not present in ${REPO_DIR}. This script never fetches; make the approved commit available first."
require_clean_source "${REPO_DIR}"
log "commit present and source tree clean"

# ---------------------------------------------------------------------------
stage "3/16  Acquiring the deployment lock"
if [ "${DRY_RUN}" -eq 0 ]; then
    mkdir -p "$(dirname "${LOCK_FILE}")" 2>/dev/null || true
    acquire_deploy_lock "${LOCK_FILE}"
else
    log "dry run: lock not taken"
fi

# ---------------------------------------------------------------------------
stage "4/16  Validating host layout"
if [ "${SKIP_MOUNT_CHECK}" != "1" ]; then
    require_mountpoint "${DATA_ROOT}"
else
    warn "mount check skipped (RADIO_SKIP_MOUNT_CHECK=1); intended for non-production validation only"
fi

RELEASE_ROOT="${RADIO_RELEASE_ROOT:-${DATA_ROOT}/releases}"
DEPLOY_ROOT="${RADIO_DEPLOY_ROOT:-${DATA_ROOT}/deploy}"

# ---------------------------------------------------------------------------
stage "5/16  Loading Compose environment"
require_env_file "${COMPOSE_ENV}"
# Only non-secret interpolation settings live here, so sourcing is safe. The
# container env files are NEVER sourced -- Compose reads them directly.
set -a
# shellcheck disable=SC1090
source "${COMPOSE_ENV}"
set +a
log "loaded ${COMPOSE_ENV} (contents not printed)"

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-radio-prod}"
RADIO_API_PUBLISH_HOST="${RADIO_API_PUBLISH_HOST:-127.0.0.1}"
RADIO_ALLOW_DIRECT_HTTP="${RADIO_ALLOW_DIRECT_HTTP:-0}"
RELEASE_ROOT="${RADIO_RELEASE_ROOT:-${RELEASE_ROOT}}"
DEPLOY_ROOT="${RADIO_DEPLOY_ROOT:-${DEPLOY_ROOT}}"

# ---------------------------------------------------------------------------
stage "6/16  Resolving container runtime identity"
HOST_IDENTITY="$(resolve_host_identity radio)"
if [ -n "${HOST_IDENTITY}" ]; then
    read -r HOST_UID HOST_GID <<<"${HOST_IDENTITY}"
    log "host 'radio' account is ${HOST_UID}:${HOST_GID}"
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

# ---------------------------------------------------------------------------
stage "7/16  Validating host directories"
require_writable_ownership "${RADIO_CONTAINER_UID}" "${RADIO_CONTAINER_GID}" \
    "${DATA_ROOT}/database" "${DATA_ROOT}/spool" "${DATA_ROOT}/evidence" \
    "${DATA_ROOT}/logs" "${DATA_ROOT}/backups"
mkdir -p "${RELEASE_ROOT}" "${DEPLOY_ROOT}/history" "${DEPLOY_ROOT}/logs"

# ---------------------------------------------------------------------------
stage "8/16  Validating environment files"
require_env_file "${ENV_DIR}/infrastructure.env"
require_env_file "${ENV_DIR}/application.env"
reject_placeholder_secret "${ENV_DIR}/application.env"
reject_static_aws_credentials "${ENV_DIR}/infrastructure.env" "${ENV_DIR}/application.env"
log "environment files present with safe permissions (contents not printed)"

# ---------------------------------------------------------------------------
stage "9/16  Validating exposure policy"
validate_publish_host "${RADIO_API_PUBLISH_HOST}" "${RADIO_ALLOW_DIRECT_HTTP}"
export RADIO_API_PUBLISH_HOST
log "API publish host: ${RADIO_API_PUBLISH_HOST}"

# ---------------------------------------------------------------------------
stage "10/16 Checking disk space"
require_free_space / "${ROOT_FREE_MIB}"
require_free_space "${DATA_ROOT}" "${DATA_FREE_MIB}"

# ---------------------------------------------------------------------------
stage "11/16 Creating the immutable release"
if [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: would create ${RELEASE_ROOT}/${COMMIT} via git archive"
    RELEASE_DIR="${RELEASE_ROOT}/${COMMIT}"
else
    RELEASE_DIR="$(create_release "${REPO_DIR}" "${COMMIT}" "${RELEASE_ROOT}")"
    write_release_manifest "${RELEASE_DIR}" "${COMMIT}" "${STAGE}"
    log "release at ${RELEASE_DIR}"

    if [ -x "${RELEASE_DIR}/scripts/secret-scan.sh" ]; then
        ( cd "${RELEASE_DIR}" && bash scripts/secret-scan.sh >/dev/null ) \
            || die "${EXIT_PRECONDITION}" "secret scan failed inside release ${COMMIT}"
        log "release secret scan passed"
    fi
fi

# Profiles per stage. `full` is never the default: it starts live capture.
case "${STAGE}" in
    api)  PROFILES=(--profile core) ; SERVICES=(api) ;;
    core) PROFILES=(--profile core) ; SERVICES=(api planner) ;;
    full) PROFILES=(--profile core --profile pipeline --profile llm)
          SERVICES=(api planner listener transcription-worker analysis-worker cleanup-worker llm) ;;
esac

COMPOSE_FILES=(-f "${RELEASE_DIR}/compose.yaml" -f "${RELEASE_DIR}/compose.prod.yaml")
compose() { docker compose --project-name "${COMPOSE_PROJECT_NAME}" "${COMPOSE_FILES[@]}" "$@"; }

# ---------------------------------------------------------------------------
stage "12/16 Validating the rendered Compose configuration"
if [ "${DRY_RUN}" -eq 1 ] && [ ! -d "${RELEASE_DIR}" ]; then
    log "dry run: release not materialised, skipping compose config"
else
    compose "${PROFILES[@]}" config >/dev/null \
        || die "${EXIT_PRECONDITION}" "compose config failed for release ${COMMIT}"
    log "compose configuration valid"
fi

# ---------------------------------------------------------------------------
stage "13/16 Verifying models"
if [ "${STAGE}" = "full" ]; then
    MODEL_ROOT="${RADIO_HOST_MODELS:-${DATA_ROOT}/models}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "dry run: would verify models under ${MODEL_ROOT}"
    else
        python3 "${RELEASE_DIR}/scripts/verify-models.py" --root "${MODEL_ROOT}" \
            || die "${EXIT_PRECONDITION}" \
               "model verification failed; run scripts/download-models.py --root ${MODEL_ROOT} first. Models are never downloaded by this script."
        log "models verified"
    fi
else
    log "stage ${STAGE} needs no models"
fi

# Exact-SHA image tags. `latest`/`local`/a branch tag would make the running
# image ambiguous and rollback unverifiable.
export RADIO_API_IMAGE="radio-api:${COMMIT}"
export RADIO_PIPELINE_IMAGE="radio-pipeline:${COMMIT}"
export RADIO_LLM_IMAGE="radio-llm:${COMMIT}"
log "image tags pinned to ${COMMIT}"

if [ "${DRY_RUN}" -eq 1 ]; then
    stage "Dry run complete"
    log "every validation gate passed; nothing was built, started or changed"
    exit "${EXIT_OK}"
fi

DEPLOY_LOG="${DEPLOY_ROOT}/logs/deploy-${COMMIT}-$(date -u +%Y%m%dT%H%M%SZ).log"
STATE_FILE="${DEPLOY_ROOT}/state.json"
PREVIOUS_COMMIT="$(read_release_target "${RELEASE_ROOT}/current" 2>/dev/null || true)"
CONTAINERS_TOUCHED=0

RECOVERY_RESULT="not-attempted"

# Restore the previous release in-process.
#
# Deliberately NOT a call to rollback-compose.sh: that script takes the same
# deployment lock this process already holds, so invoking it here would either
# deadlock or require dropping the lock mid-failure and letting another deploy
# in while the host is half-changed.
#
# Never rebuilds. The previous images already exist and were already reviewed;
# building a fresh one during an incident produces an artifact nobody has seen.
# Never restores SQLite -- see the header.
restore_previous_release() {
    local target_dir="${RELEASE_ROOT}/${PREVIOUS_COMMIT}"
    local prev_stage required
    local prev_profiles=() prev_services=()

    [ -d "${target_dir}" ] || { fail "previous release ${target_dir} is missing"; return 1; }
    for required in compose.yaml compose.prod.yaml scripts/smoke-test.sh; do
        [ -e "${target_dir}/${required}" ] \
            || { fail "previous release lacks ${required}"; return 1; }
    done

    export RADIO_API_IMAGE="radio-api:${PREVIOUS_COMMIT}"
    export RADIO_PIPELINE_IMAGE="radio-pipeline:${PREVIOUS_COMMIT}"
    export RADIO_LLM_IMAGE="radio-llm:${PREVIOUS_COMMIT}"
    docker image inspect "${RADIO_API_IMAGE}" >/dev/null 2>&1 || {
        fail "image ${RADIO_API_IMAGE} is absent; recovery never rebuilds, so this must be finished by hand"
        return 1
    }

    prev_stage="$(read_state_field "${STATE_FILE}" stage)"
    case "${prev_stage:-api}" in
        core) prev_profiles=(--profile core)
              prev_services=(api planner) ;;
        full) prev_profiles=(--profile core --profile pipeline --profile llm)
              prev_services=(api planner listener transcription-worker analysis-worker cleanup-worker llm) ;;
        *)    prev_profiles=(--profile core)
              prev_services=(api) ;;
    esac

    # wait_for_health reads this array at call time.
    COMPOSE_FILES=(-f "${target_dir}/compose.yaml" -f "${target_dir}/compose.prod.yaml")

    log "restoring ${PREVIOUS_COMMIT} (stage ${prev_stage:-api}) without rebuilding"
    compose "${prev_profiles[@]}" up -d --remove-orphans "${prev_services[@]}" 2>&1 \
        | tee -a "${DEPLOY_LOG}" || { fail "compose up failed during recovery"; return 1; }

    wait_for_health "${EXIT_ROLLBACK}" 300 "${prev_services[@]}" \
        || { fail "recovered containers did not become healthy"; return 1; }

    bash "${target_dir}/scripts/smoke-test.sh" "http://127.0.0.1:8788" 2>&1 \
        | tee -a "${DEPLOY_LOG}" || { fail "smoke test failed after recovery"; return 1; }

    return 0
}

on_failure() {
    local code=$?
    [ "${code}" -eq 0 ] && return 0
    trap - EXIT  # never re-enter this handler

    if [ "${CONTAINERS_TOUCHED}" -eq 0 ]; then
        fail "deployment failed before any running service was changed; the current release is untouched"
        [ -n "${RELEASE_DIR:-}" ] && log "release ${RELEASE_DIR} left in place for inspection"
        exit "${code}"
    fi

    fail "deployment failed AFTER containers began changing"
    fail "the database was NOT restored; the pre-migration backup is preserved${BACKUP_PATH:+ at ${BACKUP_PATH}}"

    if [ -z "${PREVIOUS_COMMIT}" ]; then
        # First deployment on this host. There is no previous release to return
        # to, so leaving half-started services running would present a broken
        # stack as if it were deployed. Remove what this run started -- and
        # nothing else. No -v: the database and spool are bind mounts and must
        # survive for investigation.
        warn "first deployment on this host: no previous release exists to restore"
        log "stopping and removing the services this deployment started"
        compose "${PROFILES[@]}" down --remove-orphans 2>&1 | tee -a "${DEPLOY_LOG}" \
            || warn "could not fully remove the failed services; inspect 'docker compose ps' by hand"
        RECOVERY_RESULT="first-deployment-cleaned-up"
        log "release directory, database backup and deploy log are preserved for investigation"
        log "current/previous symlinks were not moved and no deployment state was recorded"
    else
        warn "attempting automatic recovery to ${PREVIOUS_COMMIT} (code and images only)"
        # Subshell: wait_for_health calls die() on a container that can never
        # become healthy, and that exit must end the recovery attempt, not this
        # handler -- the outcome still has to be recorded below.
        if ( restore_previous_release ); then
            RECOVERY_RESULT="recovered-to-${PREVIOUS_COMMIT}"
            log "automatic recovery succeeded; ${PREVIOUS_COMMIT} is serving again"
            warn "the database was NOT rolled back and still carries any migration this deployment applied"
        else
            RECOVERY_RESULT="recovery-failed"
            fail "automatic recovery FAILED; the host is not serving a verified release"
            fail "recover by hand with: scripts/rollback-compose.sh --to-commit ${PREVIOUS_COMMIT}"
        fi
    fi

    write_state_atomic \
        "${DEPLOY_ROOT}/history/failed-${COMMIT}-$(date -u +%Y%m%dT%H%M%SZ).json" \
        "$(cat <<EOF
{
  "schema_version": 1,
  "outcome": "failed",
  "attempted_commit": "${COMMIT}",
  "previous_commit": "${PREVIOUS_COMMIT}",
  "stage": "${STAGE}",
  "exit_code": ${code},
  "failed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "containers_touched": true,
  "recovery": "${RECOVERY_RESULT}",
  "database_restored": false,
  "backup_path": "${BACKUP_PATH:-}",
  "deploy_log": "${DEPLOY_LOG}"
}
EOF
)" || warn "could not record the failure report"

    exit "${code}"
}
trap on_failure EXIT

# ---------------------------------------------------------------------------
stage "14/16 Building images"
compose "${PROFILES[@]}" build 2>&1 | tee -a "${DEPLOY_LOG}" \
    || die "${EXIT_BUILD}" "image build failed"
API_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${RADIO_API_IMAGE}" 2>/dev/null || echo '')"
log "api image ${RADIO_API_IMAGE} (${API_IMAGE_ID:0:19})"

# ---------------------------------------------------------------------------
stage "15/16 Backing up and migrating the database"
DATABASE_FILE="${DATA_ROOT}/database/radio.db"
BACKUP_PATH=""
if [ -f "${DATABASE_FILE}" ]; then
    BACKUP_PATH="$(RADIO_DATABASE_PATH="${DATABASE_FILE}" \
        RADIO_HOST_BACKUPS="${DATA_ROOT}/backups" \
        bash "${RELEASE_DIR}/scripts/backup-sqlite.sh" 2>&1 | tee -a "${DEPLOY_LOG}" \
        | awk '/^    \// {print $1; exit}')" || die "${EXIT_MIGRATION}" "database backup failed"
    log "backup taken before migration"
else
    log "no existing database; skipping backup"
fi

bash "${RELEASE_DIR}/scripts/migrate-db.sh" \
    --image "${RADIO_API_IMAGE}" \
    --data-root "${DATA_ROOT}" \
    --env-dir "${ENV_DIR}" \
    --uid "${RADIO_CONTAINER_UID}" --gid "${RADIO_CONTAINER_GID}" \
    2>&1 | tee -a "${DEPLOY_LOG}" || die "${EXIT_MIGRATION}" "database migration failed"

# ---------------------------------------------------------------------------
stage "16/16 Starting services and verifying health"
CONTAINERS_TOUCHED=1
compose "${PROFILES[@]}" up -d --remove-orphans "${SERVICES[@]}" 2>&1 | tee -a "${DEPLOY_LOG}" \
    || die "${EXIT_HEALTH}" "compose up failed"

log "waiting for container health"
if ! wait_for_health "${EXIT_HEALTH}" 300 "${SERVICES[@]}"; then
    compose "${PROFILES[@]}" logs --tail=80 2>&1 | tee -a "${DEPLOY_LOG}" || true
    die "${EXIT_HEALTH}" "containers did not become healthy within 300s"
fi
log "all selected services healthy"

bash "${RELEASE_DIR}/scripts/smoke-test.sh" "http://127.0.0.1:8788" 2>&1 | tee -a "${DEPLOY_LOG}" \
    || die "${EXIT_SMOKE}" "smoke test failed against the new release"

# ---------------------------------------------------------------------------
stage "Recording deployment state"
[ -n "${PREVIOUS_COMMIT}" ] && point_symlink_atomic "${RELEASE_ROOT}/previous" "${RELEASE_ROOT}/${PREVIOUS_COMMIT}"
point_symlink_atomic "${RELEASE_ROOT}/current" "${RELEASE_DIR}"

write_state_atomic "${STATE_FILE}" "$(cat <<EOF
{
  "schema_version": 1,
  "current_commit": "${COMMIT}",
  "previous_commit": "${PREVIOUS_COMMIT}",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "deployed_by": "$(id -un)",
  "stage": "${STAGE}",
  "compose_project": "${COMPOSE_PROJECT_NAME}",
  "release_path": "${RELEASE_DIR}",
  "api_image": "${RADIO_API_IMAGE}",
  "pipeline_image": "${RADIO_PIPELINE_IMAGE}",
  "llm_image": "${RADIO_LLM_IMAGE}",
  "api_image_id": "${API_IMAGE_ID}",
  "migration": "ok",
  "backup_path": "${BACKUP_PATH}",
  "smoke_test": "pass",
  "publish_host": "${RADIO_API_PUBLISH_HOST}",
  "container_uid": ${RADIO_CONTAINER_UID},
  "container_gid": ${RADIO_CONTAINER_GID}
}
EOF
)"
cp -f "${STATE_FILE}" "${DEPLOY_ROOT}/history/state-${COMMIT}-$(date -u +%Y%m%dT%H%M%SZ).json" 2>/dev/null || true

trap - EXIT
stage "Deployment complete"
log "commit ${COMMIT} live, stage ${STAGE}, project ${COMPOSE_PROJECT_NAME}"
log "log: ${DEPLOY_LOG}"
exit "${EXIT_OK}"
