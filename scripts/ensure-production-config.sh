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
MIGRATED=()

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
# An EMPTY application.env is not configuration to preserve -- it is the wreckage
# of an interrupted first install. The never-overwrite rule exists to stop a LIVE
# audio token secret being rotated out from under every URL already issued, and a
# zero-byte file has no secret to protect. Treating it as real configuration
# would wedge the host permanently: the file exists, so it is never created, so
# the secret is never generated, so every future deployment fails validation on a
# file the deployment itself left behind.
if [ -f "${APPLICATION}" ] && [ ! -s "${APPLICATION}" ]; then
    warn "${APPLICATION} exists but is empty; an interrupted install left it, so it is not preserved"
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "dry run: would discard the empty file and create it properly"
    else
        rm -f "${APPLICATION}"
    fi
fi
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
        # Written to a temporary file and moved into place, so application.env
        # either does not exist or is complete -- never a half-written file that
        # the next run would faithfully preserve.
        staged="${APPLICATION}.new.$$"
        install -m 0640 -o root -g radio /dev/null "${staged}" 2>/dev/null \
            || install -m 0640 /dev/null "${staged}"
        # Substituted with python, not sed: the generated value can contain `/`
        # and `&`, both of which sed would interpret.
        #
        # Every occurrence is replaced, so the template must name the marker
        # exactly once and only as the value of RADIO_AUDIO_TOKEN_SECRET. A
        # comment that mentioned it would otherwise be handed the real secret
        # and application.env would carry it in a line nobody reads. The check
        # names the offending lines, because "expected exactly one" without a
        # line number sends the next person hunting through the template.
        SECRET_VALUE="${secret}" python3 -c '
import os, sys
marker, source, destination = sys.argv[1], sys.argv[2], sys.argv[3]
with open(source, encoding="utf-8") as handle:
    text = handle.read()
lines = text.splitlines()
found = [n for n, line in enumerate(lines, 1) if marker in line]
if len(found) != 1:
    where = ", ".join(str(n) for n in found) if found else "nowhere"
    raise SystemExit(
        source + ": the secret placeholder must appear exactly once, found "
        + str(len(found)) + " (line " + where + "). Every occurrence is "
        + "replaced, so naming it in a comment would write the generated "
        + "secret into that comment."
    )
if lines[found[0] - 1].strip() != "RADIO_AUDIO_TOKEN_SECRET=" + marker:
    raise SystemExit(
        source + ": line " + str(found[0]) + " must be exactly "
        + "RADIO_AUDIO_TOKEN_SECRET=<placeholder>, so the substitution can "
        + "only ever produce a secret assignment."
    )
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(text.replace(marker, os.environ["SECRET_VALUE"]))
' "${SECRET_MARKER}" "${TEMPLATE}" "${staged}" \
            || { rm -f "${staged}"; die "${EXIT_PRECONDITION}" "the application template cannot be safely substituted"; }
        unset secret
        chmod 0640 "${staged}"
        chown root:radio "${staged}" 2>/dev/null || warn "could not set root:radio on ${APPLICATION}"
        mv -f "${staged}" "${APPLICATION}"
        CREATED+=("application.env")
        log "application.env created with a freshly generated audio token secret (value not printed)"
    fi
fi

if [ -f "${APPLICATION}" ]; then
    # Settings removed with the legacy pipeline. The API refuses to start when
    # one is present -- deliberately, so a stale file is never silently ignored.
    # Here, on the deployment path, we can do better than fail: strip the dead
    # lines, say exactly what was removed, and keep a timestamped copy of the
    # original. Leaving them would turn every deployment of this release into a
    # guaranteed outage for a line that no longer does anything.
    REMOVED_SETTINGS="RADIO_PIPELINE_MODE RADIO_MAX_ACTIVE_STATIONS RADIO_RECONCILER_POLL_SECONDS"
    stale_found=0
    for name in ${REMOVED_SETTINGS}; do
        if grep -qE "^[[:space:]]*${name}=" "${APPLICATION}" 2>/dev/null; then
            stale_found=1
            warn "${APPLICATION} sets ${name}, which no longer exists"
        fi
    done
    if [ "${stale_found}" -eq 1 ]; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            log "dry run: would remove the obsolete settings above"
        else
            backup="${APPLICATION}.pre-single-pipeline.$(date -u +%Y%m%dT%H%M%SZ)"
            cp -p "${APPLICATION}" "${backup}"
            chmod 0640 "${backup}"
            for name in ${REMOVED_SETTINGS}; do
                sed -i "/^[[:space:]]*${name}=/d" "${APPLICATION}"
            done
            log "removed obsolete settings; original kept at ${backup}"
            log "see docs/architecture/adr/ADR-single-shared-sqs-pipeline.md"
            MIGRATED+=("application.env")
        fi
    fi

    require_env_file "${APPLICATION}"
    reject_placeholder_secret "${APPLICATION}"
    reject_static_aws_credentials "${APPLICATION}"
    grep -q "${SECRET_MARKER}" "${APPLICATION}" \
        && die "${EXIT_PRECONDITION}" "${APPLICATION} still contains the ${SECRET_MARKER} marker"

    # Capacity sanity. This host is verified for one unique live station; a
    # value raised without measuring throughput fills the spool and loses audio.
    #
    # The ceiling is the DEFAULT, not a wall. The owner can lift it with an
    # explicit acknowledgement in the same file -- the same idiom as
    # RADIO_ALLOW_DIRECT_HTTP for the 0.0.0.0 exposure: the dangerous thing is
    # never done silently, and never refused to someone who has said, in
    # writing, that they understand what they are accepting. The application
    # itself still refuses values above 512, which is the hard bound of the
    # listener's session model, not a policy.
    capacity="$(sed -nE 's/^[[:space:]]*RADIO_MAX_ACTIVE_UNIQUE_STATIONS=([0-9]+).*/\1/p' \
        "${APPLICATION}" | tail -1)"
    if [ -n "${capacity}" ]; then
        max_allowed="${RADIO_MAX_ALLOWED_STATION_CAPACITY:-8}"
        capacity_ack="$(sed -nE 's/^[[:space:]]*RADIO_ALLOW_UNBENCHMARKED_CAPACITY=(.*)$/\1/p' \
            "${APPLICATION}" | tail -1)"
        if [ "${capacity}" -gt 512 ]; then
            # Not a policy: the Settings model refuses it at container start,
            # so accepting it here would only move the failure somewhere less
            # helpful. No acknowledgement changes what the application boots.
            fail "RADIO_MAX_ACTIVE_UNIQUE_STATIONS=${capacity}: the application refuses values above 512"
            remediation "set RADIO_MAX_ACTIVE_UNIQUE_STATIONS and RADIO_LISTENER_MAX_SESSIONS to 512 or less in ${APPLICATION}"
            die "${EXIT_PRECONDITION}" "station capacity above the application's hard bound"
        fi
        if [ "${capacity}" -gt "${max_allowed}" ]; then
            if [ "${capacity_ack}" != "1" ]; then
                fail "RADIO_MAX_ACTIVE_UNIQUE_STATIONS=${capacity} exceeds the benchmarked ceiling of ${max_allowed} for this host"
                {
                    printf '\n'
                    printf '  Nothing above %s live stations has been measured on this host.\n' "${max_allowed}"
                    printf '  Beyond it, transcription lag and spool growth are unverified, and a\n'
                    printf '  full spool drops audio. To accept that risk explicitly, add to %s:\n\n' "${APPLICATION}"
                    printf '    RADIO_ALLOW_UNBENCHMARKED_CAPACITY=1\n\n'
                    printf '  The application itself refuses values above 512.\n\n'
                } >&2
                die "${EXIT_PRECONDITION}" "unbenchmarked station capacity requires explicit acknowledgement"
            fi
            warn "running ${capacity} live-station capacity, above the benchmarked ceiling of ${max_allowed} (explicitly acknowledged)"
            warn "watch transcription backlog and spool pressure; a full spool drops audio"
        fi
        log "active unique station capacity: ${capacity}"
    fi
fi

# ---------------------------------------------------------------------------
stage "3/4  Ensuring compose.env"
# Same reasoning as application.env: an empty file is an interrupted install, not
# an operator's choice, and preserving it would wedge every later deployment.
if [ -f "${COMPOSE_ENV}" ] && [ ! -s "${COMPOSE_ENV}" ]; then
    warn "${COMPOSE_ENV} exists but is empty; an interrupted install left it, so it is not preserved"
    [ "${DRY_RUN}" -eq 1 ] || rm -f "${COMPOSE_ENV}"
fi
if [ -f "${COMPOSE_ENV}" ]; then
    log "compose.env already exists; preserving it"
    PRESERVED+=("compose.env")
elif [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: would create ${COMPOSE_ENV}"
else
    log "creating compose.env"
    staged_compose="${COMPOSE_ENV}.new.$$"
    install -m 0640 -o root -g radio /dev/null "${staged_compose}" 2>/dev/null \
        || install -m 0640 /dev/null "${staged_compose}"
    cat > "${staged_compose}" <<EOF
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
    chmod 0640 "${staged_compose}"
    chown root:radio "${staged_compose}" 2>/dev/null || warn "could not set root:radio on ${COMPOSE_ENV}"
    mv -f "${staged_compose}" "${COMPOSE_ENV}"
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
stage "4/4  Structural validation (stdlib only)"
# LAYER A of two. Deliberately stdlib-only and deliberately NOT `from app.config
# import Settings`:
#
#   * this runs before any image is built, on the host's bare python, which has
#     no FastAPI and no pydantic;
#   * importing from the source working tree would validate against whatever is
#     checked out, which after a fetch that did not move the working tree is not
#     the commit being deployed.
#
# LAYER B runs the real Settings model inside the freshly built
# radio-api:<commit> image -- see deploy-compose.sh -- which is the only place
# the answer is about the code that will actually run.
if [ "${DRY_RUN}" -eq 1 ] || [ ! -f "${APPLICATION}" ]; then
    log "dry run or missing application.env: skipping structural validation"
else
    APP_ENV_PATH="${APPLICATION}" INFRA_ENV_PATH="${INFRASTRUCTURE}" \
    python3 -c '
import os, sys

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

merged = {**load(os.environ["INFRA_ENV_PATH"]), **load(os.environ["APP_ENV_PATH"])}
problems = []

for name in ("AWS_REGION", "RADIO_S3_BUCKET", "RADIO_AUDIO_TOKEN_SECRET"):
    if not merged.get(name):
        problems.append(f"{name} is missing or empty")

secret = merged.get("RADIO_AUDIO_TOKEN_SECRET", "")
if len(secret) < 32:
    problems.append("RADIO_AUDIO_TOKEN_SECRET is shorter than 32 characters")

for name in ("RADIO_MAX_ACTIVE_UNIQUE_STATIONS", "RADIO_LISTENER_MAX_SESSIONS",
             "RADIO_LISTENER_SHARD_COUNT", "RADIO_LISTENER_SHARD_INDEX"):
    raw = merged.get(name)
    if raw is None:
        continue
    if not raw.isdigit():
        problems.append(f"{name} is not a non-negative integer")

if problems:
    # The problem, never the value. This file holds the audio token secret.
    for problem in problems:
        print(f"configuration problem: {problem}", file=sys.stderr)
    raise SystemExit(1)

# Plain concatenation, not an f-string: this program is embedded in single
# quotes, so a nested double quote has to be backslash-escaped, and a backslash
# inside an f-string expression is a SyntaxError before Python 3.12. The host
# runs the system python3, which is older than that.
queue = merged.get("RADIO_QUEUE_BACKEND", "<default>")
capacity = merged.get("RADIO_MAX_ACTIVE_UNIQUE_STATIONS", "<default>")
print("structural checks passed: queue=" + queue + " active_capacity=" + capacity)
' || die "${EXIT_PRECONDITION}" "production configuration failed structural validation"
    log "layer B (real Settings model) runs inside the exact-SHA image during deployment"
fi

log "created:   ${#CREATED[@]}${CREATED:+ (${CREATED[*]})}"
log "preserved: ${#PRESERVED[@]}${PRESERVED:+ (${PRESERVED[*]})}"
log "migrated:  ${#MIGRATED[@]}${MIGRATED:+ (${MIGRATED[*]})}"
exit "${EXIT_OK}"
