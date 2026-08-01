#!/usr/bin/env bash
# Shared helpers for deploy-compose.sh, rollback-compose.sh and migrate-db.sh.
#
# Sourced, never executed. Everything here is a validation or reporting helper;
# nothing in this file starts, stops or builds anything.
#
# Design rules that the callers depend on:
#   * every check FAILS CLOSED and prints the smallest remediation command;
#   * nothing ever prints the contents of an environment file;
#   * nothing performs a network Git operation;
#   * nothing chowns, chmods or deletes host data.

# shellcheck shell=bash

# --- exit codes ---------------------------------------------------------------
# Distinct so a caller (and a future SSM document) can branch without parsing.
readonly EXIT_OK=0
readonly EXIT_USAGE=64
readonly EXIT_PRECONDITION=65      # host/env/state is not fit to deploy
readonly EXIT_LOCKED=66            # another deployment holds the lock
readonly EXIT_BUILD=70
readonly EXIT_MIGRATION=71
readonly EXIT_HEALTH=72
readonly EXIT_SMOKE=73
readonly EXIT_ROLLBACK=74

# --- output -------------------------------------------------------------------

log()   { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
stage() { printf '\n==> %s\n' "$*"; }
warn()  { printf 'WARNING: %s\n' "$*" >&2; }
fail()  { printf 'ERROR: %s\n' "$*" >&2; }

# die <exit-code> <message...>
die() {
    local code="$1"; shift
    fail "$*"
    exit "${code}"
}

# remediation <command...> -- print, never run.
remediation() {
    printf '\n  Run this yourself after checking it is correct:\n\n    %s\n\n' "$*" >&2
}

# --- validation ---------------------------------------------------------------

# require_commands <name...>
require_commands() {
    local missing=()
    local name
    for name in "$@"; do
        command -v "${name}" >/dev/null 2>&1 || missing+=("${name}")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        die "${EXIT_PRECONDITION}" "missing required command(s): ${missing[*]}"
    fi
}

# validate_full_sha <value>
#
# A 40-hex commit id and nothing else. A branch name, a tag, or a short sha
# would all let the deployed content change under a reviewed approval -- the
# entire point of exact-commit deployment.
#
# Matched with shell globbing rather than `grep -E '^...$'`. grep anchors per
# LINE, so a value whose first line is 40 hex characters and whose second line
# is anything at all passed -- and the commit is used as a filesystem path
# component. `*[!0-9a-f]*` rejects a newline like any other character.
is_full_sha() {
    local value="${1:-}"
    [ "${#value}" -eq 40 ] || return 1
    case "${value}" in
        *[!0-9a-f]*) return 1 ;;
    esac
    return 0
}

validate_full_sha() {
    local value="${1:-}"
    if [ -z "${value}" ]; then
        die "${EXIT_USAGE}" "--commit is required (full 40-character commit sha)"
    fi
    if ! is_full_sha "${value}"; then
        die "${EXIT_USAGE}" \
            "--commit must be a full 40-character lower-case sha, got '${value}'. Branch names, tags and short shas are refused on purpose."
    fi
}

# commit_exists_locally <repo> <sha>
#
# Deliberately local-only: this script never fetches. Whoever approved the
# commit is responsible for making it present.
commit_exists_locally() {
    local repo="$1" sha="$2"
    git -C "${repo}" cat-file -e "${sha}^{commit}" 2>/dev/null
}

# require_clean_source <repo>
require_clean_source() {
    local repo="$1"
    if [ -n "$(git -C "${repo}" status --porcelain 2>/dev/null)" ]; then
        die "${EXIT_PRECONDITION}" \
            "source repository ${repo} has uncommitted changes; refusing to package an unreviewed tree"
    fi
}

# --- numeric identity ---------------------------------------------------------

# validate_uid_gid <uid> <gid>
validate_uid_gid() {
    local uid="${1:-}" gid="${2:-}" value
    for value in "${uid}" "${gid}"; do
        case "${value}" in
            ''|*[!0-9]*)
                die "${EXIT_USAGE}" "container uid/gid must be numeric, got '${value}'" ;;
        esac
        if [ "${value}" -lt 1 ]; then
            die "${EXIT_USAGE}" "container uid/gid must not be 0 (root)"
        fi
        if [ "${value}" -gt 65533 ]; then
            die "${EXIT_USAGE}" "container uid/gid above 65533 is reserved"
        fi
    done
}

# resolve_host_identity <account>
#
# Echoes "<uid> <gid>" for a host account, or nothing when it does not exist.
resolve_host_identity() {
    local account="${1:-radio}" uid gid
    uid="$(id -u "${account}" 2>/dev/null || true)"
    gid="$(id -g "${account}" 2>/dev/null || true)"
    if [ -n "${uid}" ] && [ -n "${gid}" ]; then
        printf '%s %s' "${uid}" "${gid}"
    fi
}

# require_writable_ownership <uid> <gid> <path...>
#
# Verifies the runtime user can actually write each path. NEVER chowns:
# a recursive chown over /var/lib/radio during a deploy is how a spool full of
# evidence changes owner at the worst possible moment. Report and stop.
require_writable_ownership() {
    local uid="$1" gid="$2"; shift 2
    local path owner group mode bad=0
    for path in "$@"; do
        if [ ! -d "${path}" ]; then
            fail "required directory is missing: ${path}"
            remediation "sudo install -d -o ${uid} -g ${gid} -m 0750 ${path}"
            bad=1
            continue
        fi
        owner="$(stat -c '%u' "${path}" 2>/dev/null || echo '?')"
        group="$(stat -c '%g' "${path}" 2>/dev/null || echo '?')"
        mode="$(stat -c '%a' "${path}" 2>/dev/null || echo '?')"
        if [ "${owner}" != "${uid}" ]; then
            fail "directory ${path} is owned by uid ${owner} (mode ${mode}); the container runs as uid ${uid}"
            remediation "sudo chown -R ${uid}:${gid} ${path}"
            bad=1
        elif [ "${group}" != "${gid}" ]; then
            warn "directory ${path} has gid ${group}, container gid is ${gid}"
        fi
    done
    [ "${bad}" -eq 0 ] || die "${EXIT_PRECONDITION}" "host directory ownership does not match the container runtime user"
}

# --- environment files --------------------------------------------------------

# require_env_file <path>
#
# Existence and permissions only. The contents are NEVER read into a variable
# that could be echoed, and never printed.
require_env_file() {
    local path="$1" mode
    [ -f "${path}" ] || {
        fail "required environment file is missing: ${path}"
        remediation "sudo install -m 0640 -o root -g root /dev/null ${path}"
        die "${EXIT_PRECONDITION}" "missing environment file"
    }
    mode="$(stat -c '%a' "${path}" 2>/dev/null || echo '')"
    case "${mode}" in
        600|640|400|440) ;;
        *)
            fail "environment file ${path} has permissive mode ${mode}"
            remediation "sudo chmod 0640 ${path}"
            die "${EXIT_PRECONDITION}" "unsafe environment file permissions"
            ;;
    esac
}

# reject_placeholder_secret <application.env path>
#
# Greps for the shape only and prints no value.
reject_placeholder_secret() {
    local path="$1"
    if grep -qE '^RADIO_AUDIO_TOKEN_SECRET=(replace-me|development-only|changeme|$)' "${path}" 2>/dev/null; then
        die "${EXIT_PRECONDITION}" \
            "RADIO_AUDIO_TOKEN_SECRET in ${path} is still a placeholder; generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
    fi
}

# reject_static_aws_credentials <path...>
reject_static_aws_credentials() {
    local path
    for path in "$@"; do
        [ -f "${path}" ] || continue
        if grep -qE '^[[:space:]]*(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)=.' "${path}" 2>/dev/null; then
            die "${EXIT_PRECONDITION}" \
                "${path} defines a static AWS credential; the EC2 instance role supplies credentials and a static key has no rotation or audit trail"
        fi
    done
}

# --- direct HTTP policy -------------------------------------------------------

# validate_publish_host <host> <ack>
validate_publish_host() {
    local host="${1:-127.0.0.1}" ack="${2:-0}"
    case "${host}" in
        127.0.0.1|localhost|::1)
            return 0
            ;;
        0.0.0.0)
            if [ "${ack}" != "1" ]; then
                fail "RADIO_API_PUBLISH_HOST=0.0.0.0 publishes the API on every interface."
                {
                    printf '\n'
                    printf '  The API runs with auth_mode=none and no TLS.\n'
                    printf '  Nothing in this repository can restrict who reaches it -- that has to be\n'
                    printf '  enforced outside the container (host firewall / security group).\n'
                    printf '  This is a restricted-pilot option, not the recommended architecture;\n'
                    printf '  a reverse proxy with TLS remains future work.\n\n'
                    printf '  To proceed anyway, set RADIO_ALLOW_DIRECT_HTTP=1 in compose.env.\n\n'
                } >&2
                die "${EXIT_PRECONDITION}" "direct HTTP exposure requires explicit acknowledgement"
            fi
            warn "publishing the API on 0.0.0.0 with auth_mode=none and no TLS (explicitly acknowledged)"
            return 0
            ;;
        *)
            die "${EXIT_USAGE}" \
                "RADIO_API_PUBLISH_HOST must be 127.0.0.1, localhost, ::1 or 0.0.0.0, got '${host}'"
            ;;
    esac
}

# --- disk and mount -----------------------------------------------------------

# require_mountpoint <path>
require_mountpoint() {
    local path="$1"
    if ! mountpoint -q "${path}" 2>/dev/null; then
        die "${EXIT_PRECONDITION}" \
            "${path} is not a mount point; refusing to deploy onto the root volume where the spool would fill /"
    fi
}

# require_free_space <path> <required-mib>
require_free_space() {
    local path="$1" required="$2" available
    available="$(df -Pm "${path}" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [ -z "${available}" ]; then
        die "${EXIT_PRECONDITION}" "cannot determine free space on ${path}"
    fi
    if [ "${available}" -lt "${required}" ]; then
        die "${EXIT_PRECONDITION}" \
            "${path} has ${available} MiB free, ${required} MiB required"
    fi
    log "free space on ${path}: ${available} MiB (need ${required})"
}

# --- deployment lock ----------------------------------------------------------

# acquire_deploy_lock <lock-file>
#
# flock on a dedicated descriptor. Two concurrent deployments would race on the
# release symlinks and the database backup; serialising is not optional.
acquire_deploy_lock() {
    local lock_file="$1"
    exec 9>"${lock_file}" || die "${EXIT_PRECONDITION}" "cannot open lock file ${lock_file}"
    if ! flock -n 9; then
        die "${EXIT_LOCKED}" "another deployment holds ${lock_file}; refusing to run concurrently"
    fi
    # Released automatically when the process exits, including on failure.
    log "acquired deployment lock ${lock_file}"
}

# --- stage plan ---------------------------------------------------------------
#
# THE single definition of what each stage means. deploy-compose.sh, its
# automatic recovery path, rollback-compose.sh and the tests all read it from
# here. Three independent copies of this mapping is how "rollback started the
# wrong service set" happens, and that is discovered in production.
#
# `build_services` is deliberately narrower than `runtime_services`: one service
# per image. planner is the representative build for the pipeline image, so an
# api-stage deploy never builds a pipeline or LLM image it will not run.

# stage_plan <stage> <field>
#
# Fields: profiles | runtime_services | build_services | image_repos
#         | excluded_services
stage_plan() {
    local stage="$1" field="$2"
    case "${stage}" in
        api|core|full) ;;
        *) die "${EXIT_USAGE}" "unknown stage '${stage}' (expected api, core or full)" ;;
    esac
    case "${stage}:${field}" in
        api:profiles)           printf 'core' ;;
        api:runtime_services)   printf 'api' ;;
        api:build_services)     printf 'api' ;;
        api:image_repos)        printf 'radio-api' ;;
        api:excluded_services)  printf 'planner listener transcription-worker analysis-worker cleanup-worker llm' ;;

        core:profiles)          printf 'core' ;;
        core:runtime_services)  printf 'api planner' ;;
        core:build_services)    printf 'api planner' ;;
        core:image_repos)       printf 'radio-api radio-pipeline' ;;
        core:excluded_services) printf 'listener transcription-worker analysis-worker cleanup-worker llm' ;;

        full:profiles)          printf 'core pipeline llm' ;;
        full:runtime_services)  printf 'api planner listener transcription-worker analysis-worker cleanup-worker llm' ;;
        full:build_services)    printf 'api planner llm' ;;
        full:image_repos)       printf 'radio-api radio-pipeline radio-llm' ;;
        full:excluded_services) printf '' ;;

        *) die "${EXIT_USAGE}" "unknown stage field '${field}'" ;;
    esac
}

# stage_profile_args <stage> -- "--profile core --profile pipeline ..."
stage_profile_args() {
    local profile
    for profile in $(stage_plan "$1" profiles); do
        printf -- '--profile %s ' "${profile}"
    done
}

# stage_required_images <stage> <commit> -- "radio-api:<sha> radio-pipeline:<sha>"
stage_required_images() {
    local repo
    for repo in $(stage_plan "$1" image_repos); do
        printf '%s:%s ' "${repo}" "$2"
    done
}

# require_stage_images <stage> <commit>
#
# Every image the stage needs must already exist locally. Reports ALL missing
# images rather than the first: an operator fixing them one round-trip at a
# time during an incident is exactly the wrong shape of feedback.
require_stage_images() {
    local stage="$1" commit="$2" image
    local missing=()
    for image in $(stage_required_images "${stage}" "${commit}"); do
        if ! docker image inspect "${image}" >/dev/null 2>&1; then
            missing+=("${image}")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        fail "stage ${stage} requires ${#missing[@]} image(s) that are not present locally:"
        for image in "${missing[@]}"; do
            fail "  missing: ${image}"
        done
        return 1
    fi
    log "all ${stage}-stage images present for ${commit}"
}

# build_service_for_repo <image-repo>
#
# The source is identical across stages, so images stay tagged by commit alone
# -- there is no radio-api:<sha>-core. One build service represents each image.
build_service_for_repo() {
    case "$1" in
        radio-api)      printf 'api' ;;
        radio-pipeline) printf 'planner' ;;
        radio-llm)      printf 'llm' ;;
        *) die "${EXIT_USAGE}" "no build service is defined for image ${1}" ;;
    esac
}

# missing_stage_build_services <stage> <commit>
#
# The build services whose image is NOT already present. Promoting api -> core
# on the same commit must not rebuild radio-api:<sha>: it already exists, it is
# byte-identical because the commit is identical, and rebuilding it would burn
# minutes of ARM CPU to produce a new image id for the same source -- which also
# makes the deployment history look like the API changed when it did not.
missing_stage_build_services() {
    local stage="$1" commit="$2" repo
    for repo in $(stage_plan "${stage}" image_repos); do
        if ! docker image inspect "${repo}:${commit}" >/dev/null 2>&1; then
            printf '%s ' "$(build_service_for_repo "${repo}")"
        fi
    done
}

# stage_image_ids <stage> <commit>
#
# Emits `<repo>=<image-id>` per required image, so deployment state records what
# actually ran rather than only the API image.
stage_image_ids() {
    local stage="$1" commit="$2" repo id
    for repo in $(stage_plan "${stage}" image_repos); do
        id="$(docker image inspect --format '{{.Id}}' "${repo}:${commit}" 2>/dev/null || true)"
        printf '%s=%s\n' "${repo}" "${id}"
    done
}

# json_image_field <stage> <commit> <repo> <suffix>
#
# Emits a JSON value for an image field: the quoted tag/id when the stage needs
# that image, otherwise literal null. A stage that never built an LLM image must
# not report one as if it had been verified.
json_image_field() {
    local stage="$1" commit="$2" repo="$3" what="$4" value
    case " $(stage_plan "${stage}" image_repos) " in
        *" ${repo} "*) ;;
        *) printf 'null'; return 0 ;;
    esac
    if [ "${what}" = "id" ]; then
        value="$(docker image inspect --format '{{.Id}}' "${repo}:${commit}" 2>/dev/null || true)"
        [ -n "${value}" ] || { printf 'null'; return 0; }
    else
        value="${repo}:${commit}"
    fi
    printf '"%s"' "${value}"
}

# --- release manifest ---------------------------------------------------------

# validate_release_manifest <release-dir> <expected-sha> <expected-stage>
#
# Echoes the validated stage on success; fails closed otherwise.
#
# This is what makes the exact-commit guarantee cover the MATERIALISED release
# rather than the directory name. Identity is commit AND stage, and every one of
# the three places it is recorded -- the commit directory name, the stage
# directory name, and the manifest -- must agree with what the caller asked for.
# Two agreeing sources with a third that disagrees is not a quorum; it is a
# release nobody can describe.
validate_release_manifest() {
    local dir="$1" expected="$2" expected_stage="$3"
    local manifest="${dir}/.release-manifest.json"
    local payload schema commit stage source required
    local dir_stage commit_dir dir_commit

    case "${expected_stage}" in
        api|core|full) ;;
        *)
            fail "expected stage '${expected_stage}' is not api, core or full"
            return 1
            ;;
    esac

    if [ -L "${dir}" ]; then
        fail "release path ${dir} is a symlink; refusing to follow it"
        return 1
    fi
    commit_dir="$(dirname "${dir}")"
    if [ -L "${commit_dir}" ]; then
        fail "release commit directory ${commit_dir} is a symlink; refusing to follow it"
        return 1
    fi
    dir_stage="$(basename "${dir}")"
    dir_commit="$(basename "${commit_dir}")"
    if [ "${dir_stage}" != "${expected_stage}" ]; then
        fail "release directory stage '${dir_stage}' does not match the requested stage '${expected_stage}'"
        return 1
    fi
    if [ "${dir_commit}" != "${expected}" ]; then
        fail "release commit directory '${dir_commit}' does not match the requested commit ${expected}"
        return 1
    fi
    if [ ! -d "${dir}" ]; then
        fail "release directory ${dir} does not exist"
        return 1
    fi
    if [ -L "${manifest}" ]; then
        fail "release manifest in ${dir} is a symlink"
        return 1
    fi
    if [ ! -f "${manifest}" ]; then
        fail "release ${expected} has no .release-manifest.json; refusing to act on an unverified directory"
        return 1
    fi

    payload="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        document = json.load(handle)
except Exception:
    sys.exit(1)
if not isinstance(document, dict):
    sys.exit(1)
fields = ("schema_version", "commit", "stage", "source")
print("\t".join(str(document.get(name, "")) for name in fields))
' "${manifest}" 2>/dev/null || true)"

    if [ -z "${payload}" ]; then
        fail "release manifest in ${dir} is not valid JSON"
        return 1
    fi
    IFS=$'\t' read -r schema commit stage source <<<"${payload}"

    if [ "${schema}" != "1" ]; then
        fail "unsupported manifest schema_version '${schema}' in ${dir}"
        return 1
    fi
    if ! is_full_sha "${commit}"; then
        fail "manifest commit '${commit}' is not a full lower-case 40-character sha"
        return 1
    fi
    if [ "${commit}" != "${expected}" ]; then
        fail "manifest commit ${commit} does not match the requested target ${expected}"
        return 1
    fi
    case "${stage}" in
        api|core|full) ;;
        *)
            fail "manifest stage '${stage}' is not api, core or full; refusing to guess"
            return 1
            ;;
    esac
    if [ "${stage}" != "${expected_stage}" ]; then
        fail "manifest stage '${stage}' does not match the requested stage '${expected_stage}'"
        return 1
    fi
    # Required, not merely "not contradictory". An absent source used to pass,
    # so a hand-made directory with a plausible manifest and no provenance was
    # indistinguishable from one this deployment archived itself.
    if [ "${source}" != "git archive" ]; then
        fail "manifest source '${source:-<absent>}' is not exactly 'git archive'"
        return 1
    fi

    for required in VERSION compose.yaml compose.prod.yaml scripts/smoke-test.sh; do
        if [ -L "${dir}/${required}" ]; then
            fail "${required} in release ${expected} is a symlink"
            return 1
        fi
        if [ ! -f "${dir}/${required}" ]; then
            fail "release ${expected} is missing regular file ${required}"
            return 1
        fi
    done

    printf '%s' "${stage}"
}

# --- service reconciliation ---------------------------------------------------

# reconcile_stage_services <stage>
#
# Requires the caller to have defined `compose`.
#
# --remove-orphans only removes containers Compose no longer knows about. A
# service that is still DEFINED but belongs to a profile this stage does not
# activate is not an orphan, so narrowing full -> api used to leave the
# listener, the workers and the LLM running against the newly deployed code.
#
# Never passes -v: the database, spool and evidence are bind mounts, and an
# anonymous-volume sweep during a stage change is not something a deploy should
# decide to do.
reconcile_stage_services() {
    local stage="$1" excluded service cid running
    local still=()
    excluded="$(stage_plan "${stage}" excluded_services)"
    if [ -z "${excluded}" ]; then
        log "stage ${stage} excludes no service; nothing to reconcile"
        return 0
    fi

    log "reconciling to the exact ${stage} service set"
    # Every profile is enabled here on purpose: a service in a profile this
    # stage does not activate is invisible to compose otherwise, and an
    # invisible running container is the whole problem.
    # shellcheck disable=SC2086
    compose $(stage_profile_args full) stop ${excluded} >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    compose $(stage_profile_args full) rm --force --stop ${excluded} >/dev/null 2>&1 || true

    for service in ${excluded}; do
        # shellcheck disable=SC2086
        cid="$(compose $(stage_profile_args full) ps -q "${service}" 2>/dev/null || true)"
        [ -n "${cid}" ] || continue
        running="$(docker inspect --format '{{.State.Running}}' "${cid}" 2>/dev/null || echo unknown)"
        if [ "${running}" = "true" ]; then
            still+=("${service}")
        fi
    done

    if [ "${#still[@]}" -gt 0 ]; then
        fail "services excluded from stage ${stage} are still running: ${still[*]}"
        return 1
    fi
    log "excluded services absent or stopped: ${excluded}"
}

# --- backup path contract -----------------------------------------------------

# parse_backup_path <output-file>
#
# backup-sqlite.sh prints human-readable progress AND exactly one machine
# readable `BACKUP_PATH=<abs>` line, emitted last, after compression. Parsing
# the human text was how a stale uncompressed `.db` path got recorded in
# deployment state while the file on disk was `.db.gz` -- a backup reference
# that points at nothing is worse than no reference, because it reads as one.
parse_backup_path() {
    local file="$1" count path
    count="$(grep -c '^BACKUP_PATH=' "${file}" 2>/dev/null || true)"
    count="${count:-0}"
    if [ "${count}" != "1" ]; then
        fail "expected exactly one BACKUP_PATH= line from the backup, found ${count}"
        return 1
    fi
    path="$(grep '^BACKUP_PATH=' "${file}" | head -n 1)"
    path="${path#BACKUP_PATH=}"
    case "${path}" in
        /*) ;;
        *) fail "backup path is not absolute: '${path}'"; return 1 ;;
    esac
    if [ -L "${path}" ]; then
        fail "backup path is a symlink: ${path}"
        return 1
    fi
    if [ ! -f "${path}" ]; then
        fail "backup path does not exist as a regular file: ${path}"
        return 1
    fi
    printf '%s' "${path}"
}

# --- container health ---------------------------------------------------------

# wait_for_health <exit-code-on-timeout> <timeout-seconds> <service>...
#
# Requires the caller to have defined a `compose` function.
#
# Returns 0 when every service is healthy, 1 on timeout (the caller dumps logs
# and decides what to do). Anything that can never become healthy is fatal
# immediately -- waiting 300 seconds for a container that has already exited
# just delays the same failure.
#
# "none" -- a running container with no healthcheck at all -- is FAILURE, not
# success. Every production service defines one, so `none` means either the
# healthcheck was dropped from a service definition or the wrong container is
# being inspected. Treating it as healthy is how a stack with a silently
# removed healthcheck sails through the deployment gate and the operator finds
# out from users instead.
wait_for_health() {
    local fail_code="$1" timeout="$2"; shift 2
    local services=("$@")
    local deadline service cid running health exitcode pending
    deadline=$(( $(date +%s) + timeout ))

    while :; do
        pending=0
        for service in "${services[@]}"; do
            cid="$(compose ps -q "${service}" 2>/dev/null || true)"
            if [ -z "${cid}" ]; then
                die "${fail_code}" \
                    "service ${service} has no container; it never started"
            fi

            running="$(docker inspect --format '{{.State.Running}}' "${cid}" 2>/dev/null || echo unknown)"
            exitcode="$(docker inspect --format '{{.State.ExitCode}}' "${cid}" 2>/dev/null || echo unknown)"
            health="$(docker inspect \
                --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                "${cid}" 2>/dev/null || echo unknown)"

            if [ "${running}" != "true" ]; then
                die "${fail_code}" \
                    "service ${service} is not running (exit code ${exitcode}); it will not become healthy"
            fi

            case "${health}" in
                healthy)
                    ;;
                starting|unhealthy)
                    # unhealthy is not yet fatal: a container can report
                    # unhealthy while a dependency is still coming up, and the
                    # timeout is what bounds that.
                    pending=1
                    ;;
                none)
                    die "${fail_code}" \
                        "service ${service} defines no healthcheck; refusing to record a deployment as verified on the basis of an unchecked container"
                    ;;
                *)
                    die "${fail_code}" \
                        "service ${service} reported an unrecognised health status '${health}'"
                    ;;
            esac
            [ "${pending}" -eq 1 ] && break
        done

        [ "${pending}" -eq 0 ] && return 0
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            return 1
        fi
        sleep 5
    done
}

# --- release identity ---------------------------------------------------------
#
# A release is identified by COMMIT + STAGE, not by commit alone:
#
#   /var/lib/radio/releases/<sha>/api
#   /var/lib/radio/releases/<sha>/core
#   /var/lib/radio/releases/<sha>/full
#
# Identity used to be the commit alone, which made the same reviewed commit
# undeployable at a second stage: having shipped X at `api`, widening to `core`
# hit the fail-closed check on the existing directory, and the only way out was
# to produce a different Git commit purely to change deployment scope. That
# breaks the guarantee the whole model exists for -- that what runs is exactly
# what was reviewed.
#
# Each stage directory is its own immutable release, materialised by its own
# `git archive` of the same commit. Never copied from a sibling: a copy would
# make the second release's contents depend on whatever happened to the first
# one after it was created.

# release_path <release-root> <commit> <stage>
#
# Echoes <root>/<commit>/<stage>. Both components are validated before they are
# allowed anywhere near the filesystem: they are path components, so a branch
# name, a traversal, a newline or a shell metacharacter reaching this would be a
# very bad day. validate_full_sha permits only 40 lower-case hex characters and
# the stage is a closed set, which between them exclude every such character.
release_path() {
    local root="$1" commit="$2" stage="$3"
    [ -n "${root}" ] || die "${EXIT_USAGE}" "release_path: release root is required"
    validate_full_sha "${commit}"
    case "${stage}" in
        api|core|full) ;;
        *) die "${EXIT_USAGE}" "release stage must be api, core or full (got '${stage}')" ;;
    esac
    printf '%s/%s/%s' "${root}" "${commit}" "${stage}"
}

# require_below_root <release-root> <path>
#
# The resolved path must stay under the release root, and no component from the
# root down may be a symlink -- otherwise a planted link turns "write the
# release here" into "write it anywhere on the host".
require_below_root() {
    local root="$1" path="$2" current
    case "${path}" in
        "${root}"/*) ;;
        *) fail "${path} is not below the release root ${root}"; return 1 ;;
    esac
    current="${path}"
    while [ "${current}" != "${root}" ] && [ "${current}" != "/" ]; do
        if [ -L "${current}" ]; then
            fail "release path component ${current} is a symlink"
            return 1
        fi
        current="$(dirname "${current}")"
    done
    return 0
}

# --- release packaging --------------------------------------------------------

# create_release <repo> <commit> <stage> <release-root>
#
# Echoes the final release path. Uses git archive against an explicit commit:
# no checkout, no pull, no reset, and nothing from the working tree.
create_release() {
    local repo="$1" sha="$2" stage="$3" root="$4"
    local final commit_dir
    local staging
    final="$(release_path "${root}" "${sha}" "${stage}")"
    commit_dir="${root}/${sha}"

    # Fail closed. Reusing a directory because its NAME matches the approved sha
    # would make the exact-commit guarantee cover the directory name and nothing
    # else: a half-extracted release from an interrupted deploy, or a directory
    # someone edited in place to "just fix one thing", would be deployed as if
    # it were the reviewed commit. A manifest proves nothing either -- it is a
    # file inside the very directory whose integrity is in question.
    #
    # Never delete it automatically: it may be the running release, and it is
    # evidence of whatever went wrong.
    # A SIBLING stage must never block this one -- that was the promotion
    # blocker. Only the exact commit+stage already existing is a refusal.
    if [ -d "${final}" ]; then
        local current_id previous_id
        current_id="$(read_release_identity "${root}/current" 2>/dev/null || true)"
        previous_id="$(read_release_identity "${root}/previous" 2>/dev/null || true)"
        if [ "${current_id}" = "${sha}"$'\t'"${stage}" ]; then
            die "${EXIT_PRECONDITION}" \
                "commit ${sha} is already the current release at stage ${stage}; nothing to deploy"
        fi
        if [ "${previous_id}" = "${sha}"$'\t'"${stage}" ]; then
            die "${EXIT_PRECONDITION}" \
                "commit ${sha} at stage ${stage} is the previous release; use scripts/rollback-compose.sh --to-commit ${sha} --stage ${stage} instead of redeploying it"
        fi
        die "${EXIT_PRECONDITION}" \
            "an unverified release directory already exists at ${final}. Its contents cannot be trusted to match ${sha} at stage ${stage}. Inspect it, then remove it explicitly before deploying."
    fi

    mkdir -p "${commit_dir}" \
        || die "${EXIT_PRECONDITION}" "cannot create ${commit_dir}"
    require_below_root "${root}" "${final}" \
        || die "${EXIT_PRECONDITION}" "refusing to write a release outside ${root}"

    # Staged inside the commit directory so the final move is a rename within
    # one filesystem, and so a half-extracted tree is never visible at the
    # stage path even for an instant.
    staging="$(mktemp -d "${commit_dir}/.staging-${stage}.XXXXXX")" \
        || die "${EXIT_PRECONDITION}" "cannot create staging directory under ${commit_dir}"

    if ! git -C "${repo}" archive --format=tar "${sha}" | tar -x -C "${staging}"; then
        rm -rf "${staging}"
        die "${EXIT_PRECONDITION}" "git archive failed for ${sha}"
    fi

    local required=(
        "VERSION" "compose.yaml" "compose.prod.yaml"
        "docker/api.Dockerfile" "scripts/smoke-test.sh"
    )
    local item
    for item in "${required[@]}"; do
        if [ ! -e "${staging}/${item}" ]; then
            rm -rf "${staging}"
            die "${EXIT_PRECONDITION}" "release ${sha} is missing required file ${item}"
        fi
    done

    # Belt and braces: git archive cannot include these, but a future change to
    # how releases are built must not silently start shipping secrets.
    for item in ".git" ".env" "application.env" "infrastructure.env"; do
        if [ -e "${staging}/${item}" ]; then
            rm -rf "${staging}"
            die "${EXIT_PRECONDITION}" "release ${sha} unexpectedly contains ${item}"
        fi
    done

    if ! mv -T "${staging}" "${final}" 2>/dev/null; then
        # Something appeared at ${final} between the check above and now, or the
        # rename failed outright. Either way this deployment cannot show that
        # what is there matches ${sha}, so it does not proceed as if it did.
        rm -rf "${staging}"
        die "${EXIT_PRECONDITION}" \
            "could not place release at ${final}; another process may be deploying the same commit and stage. Refusing to continue against a directory this deployment did not create."
    fi

    printf '%s' "${final}"
}

# write_release_manifest <release-path> <sha> <stage>
#
# Written exactly once, when the release directory is first created. Rewriting
# it would let a stale or tampered directory be re-stamped as freshly verified,
# which is the failure create_release now refuses outright.
write_release_manifest() {
    local release="$1" sha="$2" stage="$3"
    if [ -e "${release}/.release-manifest.json" ]; then
        die "${EXIT_PRECONDITION}" \
            "release ${sha} already carries a manifest; refusing to rewrite it"
    fi
    cat > "${release}/.release-manifest.json" <<EOF
{
  "schema_version": 1,
  "commit": "${sha}",
  "stage": "${stage}",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "created_by": "$(id -un)",
  "source": "git archive"
}
EOF
}

# --- state --------------------------------------------------------------------

# write_state_atomic <state-file> <json-body>
write_state_atomic() {
    local target="$1" body="$2" temp
    temp="$(mktemp "${target}.XXXXXX")" || die "${EXIT_PRECONDITION}" "cannot stage state file"
    printf '%s\n' "${body}" > "${temp}"
    chmod 0644 "${temp}"
    mv -f "${temp}" "${target}"
}

# read_state_field <state-file> <field>
read_state_field() {
    local file="$1" field="$2"
    [ -f "${file}" ] || return 0
    python3 - "${file}" "${field}" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.load(handle).get(sys.argv[2], "") or "")
except Exception:
    pass
PY
}

# --- symlinks -----------------------------------------------------------------

# read_release_identity <symlink> [release-root]
#
# Echoes "<commit>\t<stage>" for a release pointer, or nothing.
#
# Deliberately NOT basename: a pointer now resolves to <sha>/<stage>, so the
# basename is merely the stage. Reading it as the identity would report the
# current release as "core", and every comparison against a commit would
# silently be false.
#
# When a release root is given, a target that resolves outside it is refused --
# a `current` symlink is a thing an attacker or a mistake can repoint, and every
# later step trusts what it says.
read_release_identity() {
    local link="$1" root="${2:-}" resolved commit stage
    resolved="$(readlink -f "${link}" 2>/dev/null || true)"
    [ -n "${resolved}" ] || return 0
    if [ -n "${root}" ]; then
        case "${resolved}/" in
            "${root}"/*) ;;
            *) fail "release pointer ${link} resolves outside ${root}"; return 1 ;;
        esac
    fi
    stage="$(basename "${resolved}")"
    commit="$(basename "$(dirname "${resolved}")")"
    case "${stage}" in
        api|core|full) ;;
        *) return 0 ;;
    esac
    is_full_sha "${commit}" || return 0
    printf '%s\t%s' "${commit}" "${stage}"
}

# release_identity_commit / release_identity_stage <identity>
release_identity_commit() { printf '%s' "${1%%$'\t'*}"; }
release_identity_stage()  { printf '%s' "${1##*$'\t'}"; }

# point_symlink_atomic <link> <target>
point_symlink_atomic() {
    local link="$1" target="$2" temp
    temp="${link}.tmp.$$"
    ln -sfn "${target}" "${temp}"
    mv -T "${temp}" "${link}"
}
