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
# Before asking whether the commit is there, establish that git can read the
# repository at all -- otherwise an ownership refusal is reported as a missing
# commit and sends the operator looking for the wrong problem.
require_readable_repo "${REPO_DIR}"
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
    log "dry run: would create $(release_path "${RELEASE_ROOT}" "${COMMIT}" "${STAGE}") via git archive"
    RELEASE_DIR="$(release_path "${RELEASE_ROOT}" "${COMMIT}" "${STAGE}")"
else
    RELEASE_DIR="$(create_release "${REPO_DIR}" "${COMMIT}" "${STAGE}" "${RELEASE_ROOT}")"
    write_release_manifest "${RELEASE_DIR}" "${COMMIT}" "${STAGE}"
    log "release at ${RELEASE_DIR}"

    if [ -x "${RELEASE_DIR}/scripts/secret-scan.sh" ]; then
        ( cd "${RELEASE_DIR}" && bash scripts/secret-scan.sh >/dev/null ) \
            || die "${EXIT_PRECONDITION}" "secret scan failed inside release ${COMMIT}"
        log "release secret scan passed"
    fi
fi

# Profiles and services come from the shared stage plan in deploy-common.sh.
# `full` is never the default: it starts live capture.
#
# BUILD_SERVICES is deliberately narrower than SERVICES -- one service per
# image. An api-stage deploy must not build a pipeline or LLM image it will
# never run.
read -r -a PROFILES <<<"$(stage_profile_args "${STAGE}")"
read -r -a SERVICES <<<"$(stage_plan "${STAGE}" runtime_services)"
read -r -a BUILD_SERVICES <<<"$(stage_plan "${STAGE}" build_services)"
log "stage ${STAGE}: starting [${SERVICES[*]}], building [${BUILD_SERVICES[*]}]"

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
# The previous deployment identity is COMMIT + STAGE. Reading only the commit
# would make `api X -> core X` look like "no previous release", which would send
# a failed promotion down the first-deployment cleanup path -- tearing the stack
# down instead of restoring the api release that was serving perfectly well.
PREVIOUS_IDENTITY="$(read_release_identity "${RELEASE_ROOT}/current" "${RELEASE_ROOT}" 2>/dev/null || true)"
PREVIOUS_COMMIT=""
PREVIOUS_STAGE=""
if [ -n "${PREVIOUS_IDENTITY}" ]; then
    PREVIOUS_COMMIT="$(release_identity_commit "${PREVIOUS_IDENTITY}")"
    PREVIOUS_STAGE="$(release_identity_stage "${PREVIOUS_IDENTITY}")"
    log "previous deployment identity: ${PREVIOUS_COMMIT} at stage ${PREVIOUS_STAGE}"
    if [ "${PREVIOUS_COMMIT}" = "${COMMIT}" ]; then
        log "same-commit stage change: ${PREVIOUS_STAGE} -> ${STAGE}"
    fi
else
    log "no previous release is recorded; this is a first deployment"
fi

# Persistent-state tracking. "no container changed" is NOT the same as "nothing
# changed": a backup may exist and migrations may have been applied before a
# later one failed. Reporting the coarse version of that would tell an operator
# the database is untouched when it is not.
CONTAINERS_TOUCHED=0
BACKUP_CREATED=0
BACKUP_PATH=""
MIGRATION_STARTED=0
MIGRATION_COMPLETED=0
FAILURE_PHASE="validation"

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
    local target_dir prev_stage="${PREVIOUS_STAGE}"
    local prev_profiles=() prev_services=()

    target_dir="$(release_path "${RELEASE_ROOT}" "${PREVIOUS_COMMIT}" "${prev_stage}")"

    # The PREVIOUS release's own manifest is validated against the full previous
    # identity -- commit AND stage. Recovering a failed full deployment back to
    # an api release must start an api service set; using the attempted stage
    # would start workers against code that never shipped with them. And when
    # the commit is unchanged (api X -> core X), the stage is the ONLY thing
    # distinguishing what to restore from what just failed.
    validate_release_manifest "${target_dir}" "${PREVIOUS_COMMIT}" "${prev_stage}" >/dev/null || {
        fail "previous release ${PREVIOUS_COMMIT} at stage ${prev_stage} did not pass manifest validation"
        return 1
    }
    log "previous release manifest is valid: ${PREVIOUS_COMMIT} at stage ${prev_stage}"

    export RADIO_API_IMAGE="radio-api:${PREVIOUS_COMMIT}"
    export RADIO_PIPELINE_IMAGE="radio-pipeline:${PREVIOUS_COMMIT}"
    export RADIO_LLM_IMAGE="radio-llm:${PREVIOUS_COMMIT}"

    # Every image that stage needs, checked BEFORE a single container is
    # touched. Recovery never builds and never pulls, so a missing image cannot
    # be repaired here -- discovering that half-way through would leave the host
    # with neither release running.
    require_stage_images "${prev_stage}" "${PREVIOUS_COMMIT}" || {
        fail "recovery never rebuilds or pulls, so this must be finished by hand"
        return 1
    }

    read -r -a prev_profiles <<<"$(stage_profile_args "${prev_stage}")"
    read -r -a prev_services <<<"$(stage_plan "${prev_stage}" runtime_services)"

    # wait_for_health and reconcile_stage_services read this array at call time.
    COMPOSE_FILES=(-f "${target_dir}/compose.yaml" -f "${target_dir}/compose.prod.yaml")

    log "restoring ${PREVIOUS_COMMIT} (stage ${prev_stage}) from existing artifacts"
    compose "${prev_profiles[@]}" up -d --no-build --pull never --remove-orphans \
        "${prev_services[@]}" 2>&1 \
        | tee -a "${DEPLOY_LOG}" || { fail "compose up failed during recovery"; return 1; }

    wait_for_health "${EXIT_ROLLBACK}" 300 "${prev_services[@]}" \
        || { fail "recovered containers did not become healthy"; return 1; }

    reconcile_stage_services "${prev_stage}" \
        || { fail "could not reconcile to the ${prev_stage} service set during recovery"; return 1; }

    bash "${target_dir}/scripts/smoke-test.sh" --stage "${prev_stage}" \
        "http://127.0.0.1:8788" 2>&1 \
        | tee -a "${DEPLOY_LOG}" || { fail "smoke test failed after recovery"; return 1; }

    return 0
}

write_failure_report() {
    local code="$1"
    write_state_atomic \
        "${DEPLOY_ROOT}/history/failed-${COMMIT}-$(date -u +%Y%m%dT%H%M%SZ).json" \
        "$(cat <<EOF
{
  "schema_version": 1,
  "outcome": "failed",
  "attempted_commit": "${COMMIT}",
  "attempted_stage": "${STAGE}",
  "previous_commit": "${PREVIOUS_COMMIT}",
  "previous_stage": "${PREVIOUS_STAGE}",
  "stage": "${STAGE}",
  "exit_code": ${code},
  "failure_phase": "${FAILURE_PHASE}",
  "failed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "containers_touched": $([ "${CONTAINERS_TOUCHED}" -eq 1 ] && printf true || printf false),
  "backup_created": $([ "${BACKUP_CREATED}" -eq 1 ] && printf true || printf false),
  "backup_path": "${BACKUP_PATH}",
  "migration_started": $([ "${MIGRATION_STARTED}" -eq 1 ] && printf true || printf false),
  "migration_completed": $([ "${MIGRATION_COMPLETED}" -eq 1 ] && printf true || printf false),
  "database_restored": false,
  "release_path": "${RELEASE_DIR:-}",
  "deploy_log": "${DEPLOY_LOG}",
  "recovery": "${RECOVERY_RESULT}"
}
EOF
)" || warn "could not record the failure report"
}

# Say exactly what changed. "Nothing was touched" is only true of RUNNING
# CONTAINERS -- a backup may exist and migrations may already have been applied
# when a later one failed. Reporting the coarse version of that tells an
# operator the database is untouched when it is not, and that is the sentence
# they act on.
report_persistent_state() {
    if [ "${BACKUP_CREATED}" -eq 1 ]; then
        log "a database backup WAS created and is preserved at ${BACKUP_PATH}"
    else
        log "no database backup was created"
    fi
    if [ "${MIGRATION_COMPLETED}" -eq 1 ]; then
        warn "migrations COMPLETED; the schema is already at the new version"
    elif [ "${MIGRATION_STARTED}" -eq 1 ]; then
        warn "migration STARTED and did not complete; earlier migrations in the run may already be applied"
        warn "each migration is individually transactional, so the schema is at a consistent version -- check it with: scripts/migrate-db.sh --check-only"
    else
        log "no migration was started"
    fi
    fail "the database is NEVER restored automatically"
}

on_failure() {
    local code=$?
    [ "${code}" -eq 0 ] && return 0
    trap - EXIT  # never re-enter this handler

    if [ "${CONTAINERS_TOUCHED}" -eq 0 ]; then
        fail "deployment failed in phase '${FAILURE_PHASE}' before any RUNNING CONTAINER was changed"
        log "the current release is still serving and was not restarted"
        [ -n "${RELEASE_DIR:-}" ] && log "release ${RELEASE_DIR} left in place for inspection"
        report_persistent_state
        write_failure_report "${code}"
        exit "${code}"
    fi

    fail "deployment failed in phase '${FAILURE_PHASE}', AFTER containers began changing"
    report_persistent_state

    # A previous deployment IDENTITY, not merely a previous commit. `api X ->
    # core X` has the same commit on both sides, and treating that as "nothing
    # to go back to" would tear the stack down instead of restoring the api
    # release that was serving perfectly well.
    if [ -z "${PREVIOUS_COMMIT}" ] || [ -z "${PREVIOUS_STAGE}" ]; then
        # First deployment on this host. There is no previous release to return
        # to, so leaving half-started services running would present a broken
        # stack as if it were deployed. Remove what this run started -- and
        # nothing else. No -v: the database and spool are bind mounts and must
        # survive for investigation.
        warn "first deployment on this host: no previous release identity exists to restore"
        log "stopping and removing the services this deployment started"
        compose "${PROFILES[@]}" down --remove-orphans 2>&1 | tee -a "${DEPLOY_LOG}" \
            || warn "could not fully remove the failed services; inspect 'docker compose ps' by hand"
        RECOVERY_RESULT="first-deployment-cleaned-up"
        log "release directory, database backup and deploy log are preserved for investigation"
        log "current/previous symlinks were not moved and no deployment state was recorded"
    else
        warn "attempting automatic recovery to ${PREVIOUS_COMMIT} at stage ${PREVIOUS_STAGE} (code and images only)"
        # Subshell: wait_for_health calls die() on a container that can never
        # become healthy, and that exit must end the recovery attempt, not this
        # handler -- the outcome still has to be recorded below.
        if ( restore_previous_release ); then
            RECOVERY_RESULT="recovered-to-${PREVIOUS_COMMIT}/${PREVIOUS_STAGE}"
            log "automatic recovery succeeded; ${PREVIOUS_COMMIT} at stage ${PREVIOUS_STAGE} is serving again"
            warn "the database was NOT rolled back and still carries any migration this deployment applied"
        else
            RECOVERY_RESULT="recovery-failed"
            fail "automatic recovery FAILED; the host is not serving a verified release"
            fail "recover by hand with: scripts/rollback-compose.sh --to-commit ${PREVIOUS_COMMIT} --stage ${PREVIOUS_STAGE}"
        fi
    fi

    write_failure_report "${code}"
    exit "${code}"
}
trap on_failure EXIT

# ---------------------------------------------------------------------------
stage "14/16 Building images"
FAILURE_PHASE="build"
# Only the representative build service for each image this stage needs. An
# unrestricted `compose build` builds every service in an active profile, so an
# api-stage deploy was building the pipeline image -- minutes of ARM CPU spent
# producing an artifact the stage will not start, and one more thing that can
# fail a deployment for a reason unrelated to what was being deployed.
#
# Only the images that are actually MISSING. Promoting api X -> core X must not
# rebuild radio-api:X: it exists, it is byte-identical because the commit is
# identical, and rebuilding it would burn ARM CPU to mint a new image id for the
# same source -- making the deployment history look like the API changed when it
# did not. Nothing is ever pulled.
read -r -a MISSING_BUILDS <<<"$(missing_stage_build_services "${STAGE}" "${COMMIT}")"
if [ "${#MISSING_BUILDS[@]}" -eq 0 ]; then
    log "every ${STAGE}-stage image already exists for ${COMMIT}; nothing to build"
else
    log "building only: ${MISSING_BUILDS[*]} (of ${BUILD_SERVICES[*]})"
    compose "${PROFILES[@]}" build "${MISSING_BUILDS[@]}" 2>&1 | tee -a "${DEPLOY_LOG}" \
        || die "${EXIT_BUILD}" "image build failed"
fi

# Build succeeding is not the same as the images existing under the exact tags
# the stage will start.
require_stage_images "${STAGE}" "${COMMIT}" \
    || die "${EXIT_BUILD}" "required images are missing after a build that reported success"
while IFS='=' read -r image_repo image_id; do
    [ -n "${image_repo}" ] || continue
    log "image ${image_repo}:${COMMIT} (${image_id:0:19})"
done <<<"$(stage_image_ids "${STAGE}" "${COMMIT}")"

# ---------------------------------------------------------------------------
# LAYER B of configuration validation. The real Settings model, inside the image
# that was just built from the exact commit -- so the answer is about the code
# that will actually run, not about whatever is checked out in the source clone.
# Runs before the backup and the migration, because a configuration that cannot
# load is a deployment that must stop before it changes any persistent state.
stage "14a/16 Validating configuration against the exact image"
FAILURE_PHASE="config-validation"
docker run --rm \
    --name "radio-config-validate-$$" \
    --network none \
    --user "${RADIO_CONTAINER_UID}:${RADIO_CONTAINER_GID}" \
    --env-file "${ENV_DIR}/infrastructure.env" \
    --env-file "${ENV_DIR}/application.env" \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --entrypoint python \
    "${RADIO_API_IMAGE}" \
    -m app.cli.validate_configuration 2>&1 | tee -a "${DEPLOY_LOG}" \
    || die "${EXIT_PRECONDITION}" \
       "configuration is not valid for the code at ${COMMIT}; nothing was migrated"

# ---------------------------------------------------------------------------
stage "15/16 Backing up and migrating the database"
FAILURE_PHASE="backup"
DATABASE_FILE="${DATA_ROOT}/database/radio.db"
if [ -f "${DATABASE_FILE}" ]; then
    # Captured to a file rather than piped through awk. The old version ran
    # `awk '{print; exit}'`, which closed the pipe and SIGPIPE'd the backup
    # script mid-run -- while it was still pruning and uploading -- and recorded
    # the pre-compression `.db` path that gzip had already replaced.
    BACKUP_OUTPUT="$(mktemp)"
    if ! RADIO_DATABASE_PATH="${DATABASE_FILE}" \
         RADIO_HOST_BACKUPS="${DATA_ROOT}/backups" \
         bash "${RELEASE_DIR}/scripts/backup-sqlite.sh" >"${BACKUP_OUTPUT}" 2>&1; then
        cat "${BACKUP_OUTPUT}" >>"${DEPLOY_LOG}" 2>/dev/null || true
        cat "${BACKUP_OUTPUT}" >&2 || true
        rm -f "${BACKUP_OUTPUT}"
        die "${EXIT_MIGRATION}" "database backup failed"
    fi
    # All human-readable output is preserved; only the marker line is parsed.
    cat "${BACKUP_OUTPUT}" >>"${DEPLOY_LOG}" 2>/dev/null || true
    BACKUP_PATH="$(parse_backup_path "${BACKUP_OUTPUT}")" || {
        rm -f "${BACKUP_OUTPUT}"
        die "${EXIT_MIGRATION}" "backup completed but did not report a usable path"
    }
    rm -f "${BACKUP_OUTPUT}"
    BACKUP_CREATED=1
    log "backup taken before migration: ${BACKUP_PATH}"
else
    log "no existing database; skipping backup"
fi

FAILURE_PHASE="migration"
MIGRATION_STARTED=1
bash "${RELEASE_DIR}/scripts/migrate-db.sh" \
    --image "${RADIO_API_IMAGE}" \
    --data-root "${DATA_ROOT}" \
    --env-dir "${ENV_DIR}" \
    --uid "${RADIO_CONTAINER_UID}" --gid "${RADIO_CONTAINER_GID}" \
    2>&1 | tee -a "${DEPLOY_LOG}" || die "${EXIT_MIGRATION}" "database migration failed"
MIGRATION_COMPLETED=1

# ---------------------------------------------------------------------------
stage "16/16 Starting services and verifying health"
FAILURE_PHASE="start"
CONTAINERS_TOUCHED=1
# --no-build --pull never: this deployment has already built exactly the images
# it needs and verified they exist. Every service declares both `image` and
# `build`, and Compose's default behaviour when the tag is missing is to build
# it -- which would silently produce an unreviewed image from whatever source
# the release directory happens to hold, outside the build stage's gates.
compose "${PROFILES[@]}" up -d --no-build --pull never --remove-orphans \
    "${SERVICES[@]}" 2>&1 | tee -a "${DEPLOY_LOG}" \
    || die "${EXIT_HEALTH}" "compose up failed"

log "waiting for container health"
FAILURE_PHASE="health"
if ! wait_for_health "${EXIT_HEALTH}" 300 "${SERVICES[@]}"; then
    compose "${PROFILES[@]}" logs --tail=80 2>&1 | tee -a "${DEPLOY_LOG}" || true
    die "${EXIT_HEALTH}" "containers did not become healthy within 300s"
fi
log "all selected services healthy"

# Narrowing a stage (full -> api) leaves the excluded services running, because
# --remove-orphans only removes services Compose no longer knows about, and a
# service in an inactive profile is still defined. Reconcile before the smoke
# test so the smoke test describes the service set that will actually persist.
FAILURE_PHASE="reconcile"
reconcile_stage_services "${STAGE}" \
    || die "${EXIT_HEALTH}" "could not reconcile to the exact ${STAGE} service set"

FAILURE_PHASE="smoke"
bash "${RELEASE_DIR}/scripts/smoke-test.sh" --stage "${STAGE}" "http://127.0.0.1:8788" 2>&1 \
    | tee -a "${DEPLOY_LOG}" \
    || die "${EXIT_SMOKE}" "smoke test failed against the new release"

# ---------------------------------------------------------------------------
stage "Recording deployment state"
# Both pointers address a stage-specific release, so `previous` after
# `api X -> core X` is releases/X/api while `current` is releases/X/core. The
# two are NOT collapsed just because the commit matches -- the stage is the only
# thing that distinguishes them, and losing it would leave nothing to roll back
# to.
if [ -n "${PREVIOUS_COMMIT}" ] && [ -n "${PREVIOUS_STAGE}" ]; then
    point_symlink_atomic "${RELEASE_ROOT}/previous" \
        "$(release_path "${RELEASE_ROOT}" "${PREVIOUS_COMMIT}" "${PREVIOUS_STAGE}")"
fi
point_symlink_atomic "${RELEASE_ROOT}/current" "${RELEASE_DIR}"

write_state_atomic "${STATE_FILE}" "$(cat <<EOF
{
  "schema_version": 1,
  "current_commit": "${COMMIT}",
  "current_stage": "${STAGE}",
  "previous_commit": "${PREVIOUS_COMMIT}",
  "previous_stage": "${PREVIOUS_STAGE}",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "deployed_by": "$(id -un)",
  "_comment_stage": "legacy alias of current_stage, kept for older tooling; current_stage is authoritative",
  "stage": "${STAGE}",
  "compose_project": "${COMPOSE_PROJECT_NAME}",
  "release_path": "${RELEASE_DIR}",
  "runtime_services": "$(stage_plan "${STAGE}" runtime_services)",
  "built_services": "$(stage_plan "${STAGE}" build_services)",
  "api_image": $(json_image_field "${STAGE}" "${COMMIT}" radio-api tag),
  "api_image_id": $(json_image_field "${STAGE}" "${COMMIT}" radio-api id),
  "pipeline_image": $(json_image_field "${STAGE}" "${COMMIT}" radio-pipeline tag),
  "pipeline_image_id": $(json_image_field "${STAGE}" "${COMMIT}" radio-pipeline id),
  "llm_image": $(json_image_field "${STAGE}" "${COMMIT}" radio-llm tag),
  "llm_image_id": $(json_image_field "${STAGE}" "${COMMIT}" radio-llm id),
  "migration": "ok",
  "backup_created": $([ "${BACKUP_CREATED}" -eq 1 ] && printf true || printf false),
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
