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
validate_full_sha() {
    local value="${1:-}"
    if [ -z "${value}" ]; then
        die "${EXIT_USAGE}" "--commit is required (full 40-character commit sha)"
    fi
    if ! printf '%s' "${value}" | grep -qE '^[0-9a-f]{40}$'; then
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

# --- release packaging --------------------------------------------------------

# create_release <repo> <sha> <release-root>
#
# Echoes the final release path. Uses git archive against an explicit commit:
# no checkout, no pull, no reset, and nothing from the working tree.
create_release() {
    local repo="$1" sha="$2" root="$3"
    local final="${root}/${sha}"
    local staging

    # Fail closed. Reusing a directory because its NAME matches the approved sha
    # would make the exact-commit guarantee cover the directory name and nothing
    # else: a half-extracted release from an interrupted deploy, or a directory
    # someone edited in place to "just fix one thing", would be deployed as if
    # it were the reviewed commit. A manifest proves nothing either -- it is a
    # file inside the very directory whose integrity is in question.
    #
    # Never delete it automatically: it may be the running release, and it is
    # evidence of whatever went wrong.
    if [ -d "${final}" ]; then
        local current_target previous_target
        current_target="$(read_release_target "${root}/current" 2>/dev/null || true)"
        previous_target="$(read_release_target "${root}/previous" 2>/dev/null || true)"
        if [ "${current_target}" = "${sha}" ]; then
            die "${EXIT_PRECONDITION}" \
                "commit ${sha} is already the current release; nothing to deploy"
        fi
        if [ "${previous_target}" = "${sha}" ]; then
            die "${EXIT_PRECONDITION}" \
                "commit ${sha} is the previous release; use scripts/rollback-compose.sh --to-commit ${sha} instead of redeploying it"
        fi
        die "${EXIT_PRECONDITION}" \
            "an unverified release directory already exists at ${final}. Its contents cannot be trusted to match ${sha}. Inspect it, then remove it explicitly before deploying."
    fi

    staging="$(mktemp -d "${root}/.staging-${sha}.XXXXXX")" \
        || die "${EXIT_PRECONDITION}" "cannot create staging directory under ${root}"

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
            "could not place release at ${final}; another process may be deploying the same commit. Refusing to continue against a directory this deployment did not create."
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

# read_release_target <symlink>
#
# Echoes the commit sha a release symlink points at, or nothing.
read_release_target() {
    local link="$1" resolved
    resolved="$(readlink -f "${link}" 2>/dev/null || true)"
    [ -n "${resolved}" ] && basename "${resolved}"
}

# point_symlink_atomic <link> <target>
point_symlink_atomic() {
    local link="$1" target="$2" temp
    temp="${link}.tmp.$$"
    ln -sfn "${target}" "${temp}"
    mv -T "${temp}" "${link}"
}
