#!/usr/bin/env bash
# Ensure the three production environment files exist and are safe.
#
#   scripts/ensure-production-config.sh [--env-dir PATH] [--dry-run]
#
# Creates what is missing, ONCE. Never overwrites, never regenerates a secret,
# never prints a value. An existing file is validated and left byte-for-byte
# alone: rewriting application.env on every deployment would rotate the audio
# token secret and invalidate every URL already handed to the frontend.
#
#   infrastructure.env  non-secret AWS wiring. Must already exist -- it names
#                       real infrastructure, and guessing a bucket or queue name
#                       would point the pipeline at something that is not there.
#   application.env     created from the public template on first install, with
#                       the one secret generated.
#   compose.env         created with the approved pilot exposure on first
#                       install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

ENV_DIR="${RADIO_ENV_DIR:-/etc/radio-broadcast-analysis}"
TEMPLATE="${RADIO_APP_ENV_TEMPLATE:-${SCRIPT_DIR}/../deploy/env/application.env.example}"
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
SECRET_MARKER="REPLACE_WITH_GENERATED_SECRET"
DRY_RUN=0

CREATED=()
PRESERVED=()

usage() {
    cat <<'USAGE'
Ensure production environment files exist, without overwriting anything.

Usage:
  scripts/ensure-production-config.sh [options]

Options:
  --env-dir PATH   Environment directory (default /etc/radio-broadcast-analysis).
  --template PATH  Public application template.
  --dry-run        Report what would be created; write nothing.
  -h, --help       Show this help.

Never prints a file's contents. Never regenerates an existing secret.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --env-dir)  ENV_DIR="${2:-}"; shift 2 ;;
        --template) TEMPLATE="${2:-}"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)  usage; exit "${EXIT_OK}" ;;
        *)          usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

require_commands python3 install stat

INFRASTRUCTURE="${ENV_DIR}/infrastructure.env"
APPLICATION="${ENV_DIR}/application.env"
COMPOSE_ENV="${ENV_DIR}/compose.env"

# has_setting <file> <NAME> -- true when NAME is assigned, without printing it.
has_setting() {
    grep -qE "^[[:space:]]*${2}=" "$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
stage "1/4  Validating infrastructure.env"
# Never generated. It names the real bucket, queues and region; a guessed value
# would point the pipeline at infrastructure that does not exist, and the first
# sign of that would be silently missing audio.
if [ ! -f "${INFRASTRUCTURE}" ]; then
    fail "${INFRASTRUCTURE} is missing"
    remediation "create it from deploy/dev/infrastructure.env with the REAL production values, then: sudo chown root:radio ${INFRASTRUCTURE} && sudo chmod 0640 ${INFRASTRUCTURE}"
    die "${EXIT_PRECONDITION}" "infrastructure.env must be provisioned by an operator, never guessed"
fi
require_env_file "${INFRASTRUCTURE}"
for required in AWS_REGION RADIO_S3_BUCKET; do
    has_setting "${INFRASTRUCTURE}" "${required}" \
        || die "${EXIT_PRECONDITION}" "${INFRASTRUCTURE} does not define ${required}"
done
reject_static_aws_credentials "${INFRASTRUCTURE}"
log "infrastructure.env present and valid (contents not printed)"
PRESERVED+=("infrastructure.env")

# ---------------------------------------------------------------------------
stage "2/4  Ensuring application.env"
if [ -f "${APPLICATION}" ]; then
    log "application.env already exists; preserving it byte-for-byte"
    PRESERVED+=("application.env")
else
    [ -f "${TEMPLATE}" ] || die "${EXIT_PRECONDITION}" "template not found: ${TEMPLATE}"
    grep -q "${SECRET_MARKER}" "${TEMPLATE}" \
        || die "${EXIT_PRECONDITION}" "template ${TEMPLATE} has no ${SECRET_MARKER} marker"
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "dry run: would create ${APPLICATION} from the template with a generated secret"
    else
        log "creating application.env from the public template"
        # 48 bytes from the OS CSPRNG. Generated exactly once, in a file created
        # 0640 before anything is written into it, so the secret is never
        # momentarily world-readable.
        secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
        [ "${#secret}" -ge 48 ] || die "${EXIT_PRECONDITION}" "generated secret is too short"
        install -m 0640 -o root -g radio /dev/null "${APPLICATION}" 2>/dev/null \
            || install -m 0640 /dev/null "${APPLICATION}"
        # Substituted with python, not sed: the generated value can contain `/`
        # and `&`, both of which sed would interpret.
        SECRET_VALUE="${secret}" python3 -c '
import os, sys
marker, source, destination = sys.argv[1], sys.argv[2], sys.argv[3]
with open(source, encoding="utf-8") as handle:
    text = handle.read()
if text.count(marker) != 1:
    raise SystemExit(f"expected exactly one {marker} in the template")
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(text.replace(marker, os.environ["SECRET_VALUE"]))
' "${SECRET_MARKER}" "${TEMPLATE}" "${APPLICATION}"
        unset secret
        chmod 0640 "${APPLICATION}"
        chown root:radio "${APPLICATION}" 2>/dev/null || warn "could not set root:radio on ${APPLICATION}"
        CREATED+=("application.env")
        log "application.env created with a freshly generated audio token secret (value not printed)"
    fi
fi

if [ -f "${APPLICATION}" ]; then
    require_env_file "${APPLICATION}"
    reject_placeholder_secret "${APPLICATION}"
    reject_static_aws_credentials "${APPLICATION}"
    grep -q "${SECRET_MARKER}" "${APPLICATION}" \
        && die "${EXIT_PRECONDITION}" "${APPLICATION} still contains the ${SECRET_MARKER} marker"

    # Capacity sanity. This host is verified for one unique live station; a
    # value raised without measuring throughput fills the spool and loses audio.
    capacity="$(sed -nE 's/^[[:space:]]*RADIO_MAX_ACTIVE_UNIQUE_STATIONS=([0-9]+).*/\1/p' \
        "${APPLICATION}" | tail -1)"
    if [ -n "${capacity}" ]; then
        max_allowed="${RADIO_MAX_ALLOWED_STATION_CAPACITY:-8}"
        if [ "${capacity}" -gt "${max_allowed}" ]; then
            die "${EXIT_PRECONDITION}" \
                "RADIO_MAX_ACTIVE_UNIQUE_STATIONS=${capacity} exceeds the reviewed ceiling of ${max_allowed} for this host"
        fi
        log "active unique station capacity: ${capacity}"
    fi
fi

# ---------------------------------------------------------------------------
stage "3/4  Ensuring compose.env"
if [ -f "${COMPOSE_ENV}" ]; then
    log "compose.env already exists; preserving it"
    PRESERVED+=("compose.env")
elif [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: would create ${COMPOSE_ENV}"
else
    log "creating compose.env"
    install -m 0640 -o root -g radio /dev/null "${COMPOSE_ENV}" 2>/dev/null \
        || install -m 0640 /dev/null "${COMPOSE_ENV}"
    cat > "${COMPOSE_ENV}" <<EOF
# Compose CLI interpolation. Non-secret. Created once by
# scripts/ensure-production-config.sh and never rewritten.
COMPOSE_PROJECT_NAME=radio-prod

# RESTRICTED PILOT EXPOSURE, deliberately acknowledged.
#
# 0.0.0.0 publishes an UNAUTHENTICATED, UNENCRYPTED API on every interface.
# That is approved only because the pilot has no reverse proxy and no TLS yet,
# and access is restricted at the security group to an approved CIDR. The
# security group is the ONLY thing restricting access -- widening it exposes
# every transcript to the internet. Setting RADIO_ALLOW_DIRECT_HTTP=1 is the
# explicit acknowledgement the deployment requires before it will accept this.
RADIO_API_PUBLISH_HOST=0.0.0.0
RADIO_ALLOW_DIRECT_HTTP=1

# Empty means auto-detect the host radio account.
RADIO_CONTAINER_UID=
RADIO_CONTAINER_GID=

RADIO_RELEASE_ROOT=${DATA_ROOT}/releases
RADIO_DEPLOY_ROOT=${DATA_ROOT}/deploy
EOF
    chmod 0640 "${COMPOSE_ENV}"
    chown root:radio "${COMPOSE_ENV}" 2>/dev/null || warn "could not set root:radio on ${COMPOSE_ENV}"
    CREATED+=("compose.env")
fi

if [ -f "${COMPOSE_ENV}" ]; then
    require_env_file "${COMPOSE_ENV}"
    # Exposure is validated, never silently changed. If an operator narrowed it
    # to loopback, this must not widen it back.
    publish_host="$(sed -nE 's/^[[:space:]]*RADIO_API_PUBLISH_HOST=(.*)$/\1/p' "${COMPOSE_ENV}" | tail -1)"
    allow_direct="$(sed -nE 's/^[[:space:]]*RADIO_ALLOW_DIRECT_HTTP=(.*)$/\1/p' "${COMPOSE_ENV}" | tail -1)"
    validate_publish_host "${publish_host:-127.0.0.1}" "${allow_direct:-0}"
    log "API publish host: ${publish_host:-127.0.0.1}"
fi

# ---------------------------------------------------------------------------
stage "4/4  Validating against the real Settings model"
# Loaded through the actual pydantic model, so a typo or an out-of-range value
# fails here rather than at container start-up -- but WITHOUT starting the API,
# opening a socket or contacting AWS.
if [ "${DRY_RUN}" -eq 1 ] || [ ! -f "${APPLICATION}" ]; then
    log "dry run or missing application.env: skipping model validation"
else
    APP_ENV_PATH="${APPLICATION}" INFRA_ENV_PATH="${INFRASTRUCTURE}" \
    python3 -c '
import os, sys
sys.path.insert(0, os.environ.get("RADIO_REPO_ROOT", "."))

def load(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values

environment = {**load(os.environ["INFRA_ENV_PATH"]), **load(os.environ["APP_ENV_PATH"])}
os.environ.update(environment)
try:
    from app.config import Settings
    settings = Settings()
except Exception as error:
    # Never echo the environment: it holds the audio token secret.
    print(f"configuration is not valid: {type(error).__name__}: {error}", file=sys.stderr)
    raise SystemExit(1)
print(f"settings valid: mode={settings.RADIO_PIPELINE_MODE} "
      f"capacity={settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS}")
' || die "${EXIT_PRECONDITION}" "production configuration failed Settings validation"
fi

log "created:   ${#CREATED[@]}${CREATED:+ (${CREATED[*]})}"
log "preserved: ${#PRESERVED[@]}${PRESERVED:+ (${PRESERVED[*]})}"
exit "${EXIT_OK}"
