#!/usr/bin/env bash
# First-install-or-update entry point, invoked only by the fixed SSM document.
#
#   scripts/main-auto-deploy.sh --commit <40-hex-sha> --source-repo <abs-path>
#
# This is the whole automation surface exposed to GitHub. It accepts exactly two
# arguments and nothing else: no branch, no tag, no stage, no shell command, no
# package list, no model name. Everything else is decided here, from the
# repository content at that exact commit.
#
# What it never does:
#   * never fetches from Git itself -- the SSM document has already placed the
#     exact commit on disk, and a fetch here would be a second, unreviewed way
#     for content to arrive;
#   * never modifies AWS;
#   * never prints an environment file, a secret or a token;
#   * never restores SQLite automatically;
#   * never formats or mounts a disk.
#
# It decides between FIRST INSTALL and NORMAL UPDATE from the validated
# deployment identity, not from a flag, so the decision cannot be got wrong by
# whoever invokes it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

COMMIT=""
SOURCE_REPO=""
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
ENV_DIR="${RADIO_ENV_DIR:-/etc/radio-broadcast-analysis}"
#: A lock ABOVE the per-deployment lock. deploy-compose.sh takes its own; this
#: one stops two whole first-install-or-update runs from interleaving their
#: package installs and model downloads before either reaches that point.
INSTALL_LOCK="${RADIO_INSTALL_LOCK:-/var/lock/radio-main-auto-deploy.lock}"
ROOT_FREE_MIB="${RADIO_MIN_ROOT_FREE_MIB:-3072}"
DATA_FREE_MIB="${RADIO_MIN_DATA_FREE_MIB:-8192}"

usage() {
    cat <<'USAGE'
First-install-or-update deployment of an exact main commit.

Usage:
  scripts/main-auto-deploy.sh --commit <40-hex-sha> --source-repo <abs-path>

Required:
  --commit SHA        Full 40-character lower-case commit id. Branch names,
                      tags and short shas are refused.
  --source-repo PATH  Absolute path to the on-host clone that already contains
                      that commit. This script never fetches.

No other argument is accepted. There is deliberately no --stage, no --branch
and no way to pass a shell command.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --commit)      COMMIT="${2:-}"; shift 2 ;;
        --source-repo) SOURCE_REPO="${2:-}"; shift 2 ;;
        -h|--help)     usage; exit "${EXIT_OK}" ;;
        *)
            usage >&2
            die "${EXIT_USAGE}" "unexpected argument: $1"
            ;;
    esac
done

[ -n "${COMMIT}" ]      || { usage >&2; die "${EXIT_USAGE}" "--commit is required"; }
[ -n "${SOURCE_REPO}" ] || { usage >&2; die "${EXIT_USAGE}" "--source-repo is required"; }
case "${SOURCE_REPO}" in
    /*) ;;
    *) die "${EXIT_USAGE}" "--source-repo must be an absolute path" ;;
esac
# Shell pattern matching, not `grep -E '^...$'`: grep anchors per LINE, so a
# value whose first line is 40 hex characters and whose second line is anything
# at all would pass, and this value becomes a filesystem path component.
validate_full_sha "${COMMIT}"

# ---------------------------------------------------------------------------
stage "1/9  Validating the source repository"
# Only the commands a BASELINE Amazon Linux host already has. Requiring docker
# here was a first-install deadlock: the script demanded Docker before running
# the script whose entire job is to install Docker, so a bare host could never
# get past step one. The full toolchain is required in stage 4a, after
# ensure-host-prerequisites.sh has had its chance to provide it.
require_commands bash python3 rpm dnf mountpoint flock git
if [ -L "${SOURCE_REPO}" ]; then
    die "${EXIT_PRECONDITION}" "${SOURCE_REPO} is a symlink; refusing to follow it"
fi
[ -d "${SOURCE_REPO}/.git" ] || die "${EXIT_PRECONDITION}" "${SOURCE_REPO} is not a git repository"
if [ -L "${SOURCE_REPO}/.git" ]; then
    die "${EXIT_PRECONDITION}" "${SOURCE_REPO}/.git is a symlink; refusing to follow it"
fi

# The clone belongs to ec2-user; this script runs as root under SSM. Every git
# command goes through that account so a deployment never leaves root-owned
# objects in a repository its owner then cannot maintain -- the failure shows up
# later as a permission error during an unrelated fetch, long after the cause.
REPO_USER="${RADIO_REPO_USER:-ec2-user}"
REPO_OWNER="$(stat -c '%U' "${SOURCE_REPO}" 2>/dev/null || echo '?')"
if [ "$(id -u)" -eq 0 ] && id -u "${REPO_USER}" >/dev/null 2>&1; then
    if [ "${REPO_OWNER}" != "${REPO_USER}" ]; then
        die "${EXIT_PRECONDITION}" \
            "${SOURCE_REPO} is owned by '${REPO_OWNER}', expected ${REPO_USER}"
    fi
    repo_git() {
        runuser -u "${REPO_USER}" -- env HOME="/home/${REPO_USER}" git -C "${SOURCE_REPO}" "$@"
    }
    log "git operations run as ${REPO_USER}"
else
    # Not root, or the account does not exist: run as whoever we are. Used by
    # the Linux test harness, never by the production path.
    repo_git() { git -C "${SOURCE_REPO}" "$@"; }
    log "git operations run as $(id -un)"
fi

EXPECTED_ORIGIN="${RADIO_EXPECTED_ORIGIN:-https://github.com/naman1995jain/Radio-Broadcast-Analysis.git}"
ACTUAL_ORIGIN="$(repo_git remote get-url origin 2>/dev/null || true)"
if [ "${ACTUAL_ORIGIN}" != "${EXPECTED_ORIGIN}" ]; then
    die "${EXIT_PRECONDITION}" \
        "origin is '${ACTUAL_ORIGIN}', expected '${EXPECTED_ORIGIN}'; refusing to deploy from an unexpected source"
fi

repo_git cat-file -e "${COMMIT}^{commit}" 2>/dev/null \
    || die "${EXIT_PRECONDITION}" \
       "commit ${COMMIT} is not present in ${SOURCE_REPO}; this script never fetches"

# MAIN ONLY. The commit must be reachable from origin/main, so a commit that
# exists on the host for any other reason -- a stale fetch, a feature branch, a
# tag -- cannot be deployed. This is what makes "main is the only deployment
# source" true on the host and not merely in the workflow.
repo_git merge-base --is-ancestor "${COMMIT}" origin/main 2>/dev/null \
    || die "${EXIT_PRECONDITION}" \
       "commit ${COMMIT} is not an ancestor of origin/main; only reviewed main commits deploy"
log "commit ${COMMIT} is present and on origin/main"

# ---------------------------------------------------------------------------
stage "2/9  Acquiring the install lock"
mkdir -p "$(dirname "${INSTALL_LOCK}")" 2>/dev/null || true
# Deliberately a different lock file from the deployment lock: this one is held
# across prerequisites and model downloads, which happen before deploy-compose.sh
# takes its own. Sharing one lock would deadlock the moment it did.
exec 8>"${INSTALL_LOCK}" || die "${EXIT_PRECONDITION}" "cannot open ${INSTALL_LOCK}"
if ! flock -n 8; then
    die "${EXIT_LOCKED}" "another main-auto-deploy run holds ${INSTALL_LOCK}"
fi
log "install lock acquired"

# ---------------------------------------------------------------------------
stage "3/9  Validating the host"
require_mountpoint "${DATA_ROOT}"
require_free_space / "${ROOT_FREE_MIB}"
require_free_space "${DATA_ROOT}" "${DATA_FREE_MIB}"
log "${DATA_ROOT} mounted with sufficient space"

# ---------------------------------------------------------------------------
stage "4/9  Ensuring host prerequisites"
bash "${SCRIPT_DIR}/ensure-host-prerequisites.sh" \
    || die "${EXIT_PRECONDITION}" "host prerequisites could not be satisfied"

# ---------------------------------------------------------------------------
stage "4a/9 Requiring the full toolchain"
# Now, not before. Everything below is either installed by the step above or was
# already present, so a missing command here is a real failure rather than the
# ordering bug of asking for Docker before installing it.
require_commands git docker tar stat df awk jq curl sha256sum python3
log "full toolchain present"

# ---------------------------------------------------------------------------
stage "5/9  Ensuring runtime directories"
HOST_IDENTITY="$(resolve_host_identity radio)"
if [ -n "${HOST_IDENTITY}" ]; then
    read -r RADIO_UID RADIO_GID <<<"${HOST_IDENTITY}"
else
    die "${EXIT_PRECONDITION}" \
        "no host 'radio' account; create it before deploying (this script never creates system accounts)"
fi
for directory in database spool evidence logs backups releases deploy models; do
    path="${DATA_ROOT}/${directory}"
    if [ ! -d "${path}" ]; then
        log "creating ${path}"
        install -d -o "${RADIO_UID}" -g "${RADIO_GID}" -m 0750 "${path}"
    fi
done
require_writable_ownership "${RADIO_UID}" "${RADIO_GID}" \
    "${DATA_ROOT}/database" "${DATA_ROOT}/spool" "${DATA_ROOT}/evidence" \
    "${DATA_ROOT}/logs" "${DATA_ROOT}/backups"
log "runtime directories owned by ${RADIO_UID}:${RADIO_GID}"

# ---------------------------------------------------------------------------
stage "6/9  Ensuring production configuration"
RADIO_REPO_ROOT="${SOURCE_REPO}" bash "${SCRIPT_DIR}/ensure-production-config.sh" \
    --env-dir "${ENV_DIR}" \
    || die "${EXIT_PRECONDITION}" "production configuration could not be ensured"

# ---------------------------------------------------------------------------
stage "7/9  Determining first install or normal update"
RELEASE_ROOT="${RADIO_RELEASE_ROOT:-${DATA_ROOT}/releases}"
DEPLOY_ROOT="${RADIO_DEPLOY_ROOT:-${DATA_ROOT}/deploy}"
STATE_FILE="${DEPLOY_ROOT}/state.json"

CURRENT_COMMIT="$(read_state_field "${STATE_FILE}" current_commit 2>/dev/null || true)"
CURRENT_STAGE="$(read_state_field "${STATE_FILE}" current_stage 2>/dev/null || true)"

MODE="first-install"
if [ -n "${CURRENT_COMMIT}" ] && [ -n "${CURRENT_STAGE}" ]; then
    # A state file alone is not proof. It can be half-written by a killed
    # process, or describe a release directory that has since been removed. The
    # identity only counts when the release it names still validates -- treating
    # a partial state as a deployment would skip the first-install path and
    # deploy straight to `full` on a host with no models.
    candidate="$(release_path "${RELEASE_ROOT}" "${CURRENT_COMMIT}" "${CURRENT_STAGE}" 2>/dev/null || true)"
    if [ -n "${candidate}" ] \
       && validate_release_manifest "${candidate}" "${CURRENT_COMMIT}" "${CURRENT_STAGE}" >/dev/null 2>&1; then
        MODE="normal-update"
        log "existing deployment identity: ${CURRENT_COMMIT} at stage ${CURRENT_STAGE}"
    else
        warn "state names ${CURRENT_COMMIT}/${CURRENT_STAGE} but that release does not validate"
        warn "treating this as a FIRST INSTALL"
    fi
else
    log "no complete deployment identity recorded"
fi
log "mode: ${MODE}"

deploy_stage() {
    local target="$1"
    stage "Deploying ${COMMIT} at stage ${target}"
    bash "${SCRIPT_DIR}/deploy-compose.sh" \
        --commit "${COMMIT}" \
        --stage "${target}" \
        --repo "${SOURCE_REPO}" \
        --compose-env "${ENV_DIR}/compose.env"
}

# ---------------------------------------------------------------------------
if [ "${MODE}" = "first-install" ]; then
    stage "8/9  First install: models"
    # Every locked model, before anything starts. The full stage runs ASR and
    # the LLM, and starting those against a missing model produces a container
    # that fails its health check for a reason that has nothing to do with the
    # commit being deployed.
    bash "${SCRIPT_DIR}/ensure-models.sh" --all \
        || die "${EXIT_PRECONDITION}" "required models could not be ensured"

    stage "9/9  First install: api -> core -> full"
    # Widened one stage at a time, on the SAME commit. Each step is verified
    # before the next starts, so a host that cannot run the workers is
    # discovered while only the API is live, not after live capture has begun.
    deploy_stage api  || die "${EXIT_HEALTH}" "first install failed at stage api"
    deploy_stage core || die "${EXIT_HEALTH}" "first install failed at stage core"
    deploy_stage full || die "${EXIT_HEALTH}" "first install failed at stage full"
else
    stage "8/9  Update: verifying models"
    # Verify-only: an already valid model is never re-fetched. A locked model
    # that has gone missing is still downloaded, because the alternative is
    # starting a worker with nothing to transcribe with.
    bash "${SCRIPT_DIR}/ensure-models.sh" --verify-only --all \
        || die "${EXIT_PRECONDITION}" "model verification failed"

    stage "9/9  Update: deploying full"
    deploy_stage full || die "${EXIT_HEALTH}" "update failed at stage full"
fi

# ---------------------------------------------------------------------------
stage "Deployment complete"
FINAL_COMMIT="$(read_state_field "${STATE_FILE}" current_commit 2>/dev/null || true)"
FINAL_STAGE="$(read_state_field "${STATE_FILE}" current_stage 2>/dev/null || true)"
log "mode:    ${MODE}"
log "current: ${FINAL_COMMIT}/${FINAL_STAGE}"
log "capacity, models and database state are reported by the steps above"
printf 'MAIN_AUTO_DEPLOY_OK\n'
printf 'commit=%s\n' "${FINAL_COMMIT}"
printf 'stage=%s\n' "${FINAL_STAGE}"
printf 'mode=%s\n' "${MODE}"
exit "${EXIT_OK}"
