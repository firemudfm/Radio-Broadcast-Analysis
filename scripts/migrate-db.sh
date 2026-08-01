#!/usr/bin/env bash
# Run database migrations in a one-shot API container.
#
#   scripts/migrate-db.sh --image radio-api:<sha> [--data-root PATH]
#                         [--env-dir PATH] [--uid N] [--gid N] [--check-only]
#
# A one-shot container, not the running API:
#
#   * no port is published, so this cannot collide with a live API;
#   * no dependency service starts, so a migration cannot be blocked by the LLM
#     or a listener;
#   * the failure is unambiguous -- a non-zero exit means the migration failed,
#     not that a socket was busy.
#
# It mounts only the database and log directories. The spool, evidence and
# model directories are deliberately absent: migrating schema has no business
# touching audio.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

IMAGE=""
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
ENV_DIR="${RADIO_ENV_DIR:-/etc/radio-broadcast-analysis}"
RUN_UID=""
RUN_GID=""
CHECK_ONLY=0

usage() {
    cat <<'USAGE'
Run database migrations in a one-shot API container.

Usage:
  scripts/migrate-db.sh --image radio-api:<sha> [options]

Required:
  --image TAG        API image to run. Use an exact commit-tagged image.

Options:
  --data-root PATH   Host data root (default /var/lib/radio).
  --env-dir PATH     Environment file directory
                     (default /etc/radio-broadcast-analysis).
  --uid N            Container uid (default: host `radio`, else 10001).
  --gid N            Container gid.
  --check-only       Report the schema version, apply nothing.
  -h, --help         Show this help.

No port is published, no dependency starts, no model is downloaded.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image)      IMAGE="${2:-}"; shift 2 ;;
        --data-root)  DATA_ROOT="${2:-}"; shift 2 ;;
        --env-dir)    ENV_DIR="${2:-}"; shift 2 ;;
        --uid)        RUN_UID="${2:-}"; shift 2 ;;
        --gid)        RUN_GID="${2:-}"; shift 2 ;;
        --check-only) CHECK_ONLY=1; shift ;;
        -h|--help)    usage; exit "${EXIT_OK}" ;;
        *)            usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

[ -n "${IMAGE}" ] || { usage >&2; die "${EXIT_USAGE}" "--image is required"; }

require_commands docker stat

stage "Validating inputs"
docker image inspect "${IMAGE}" >/dev/null 2>&1 \
    || die "${EXIT_PRECONDITION}" "image ${IMAGE} is not present locally; build it before migrating"

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
log "running as ${RUN_UID}:${RUN_GID}"

DATABASE_DIR="${DATA_ROOT}/database"
LOG_DIR="${DATA_ROOT}/logs"
[ -d "${DATABASE_DIR}" ] || die "${EXIT_PRECONDITION}" "database directory ${DATABASE_DIR} does not exist"
[ -d "${LOG_DIR}" ] || die "${EXIT_PRECONDITION}" "log directory ${LOG_DIR} does not exist"

require_env_file "${ENV_DIR}/infrastructure.env"
require_env_file "${ENV_DIR}/application.env"

stage "Running migrations"
# The API image's ENTRYPOINT is uvicorn, so it must be overridden -- otherwise
# these arguments would be appended to the server command instead of replacing it.
ARGS=(-m app.cli.migrate_database)
[ "${CHECK_ONLY}" -eq 1 ] && ARGS+=(--check-only)

# --rm: this container is a command, not a service.
# --network none: schema migration needs no network at all, and denying it
#   proves the migration cannot reach S3, SQS or a model host.
docker run --rm \
    --name "radio-migrate-$$" \
    --network none \
    --user "${RUN_UID}:${RUN_GID}" \
    --env-file "${ENV_DIR}/infrastructure.env" \
    --env-file "${ENV_DIR}/application.env" \
    --env "RADIO_DATABASE_PATH=/var/lib/radio/database/radio.db" \
    --env "RADIO_LOG_PATH=/var/lib/radio/logs" \
    --volume "${DATABASE_DIR}:/var/lib/radio/database:rw" \
    --volume "${LOG_DIR}:/var/lib/radio/logs:rw" \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --entrypoint python \
    "${IMAGE}" \
    "${ARGS[@]}" \
    || die "${EXIT_MIGRATION}" "migration container exited non-zero"

stage "Migration complete"
exit "${EXIT_OK}"
