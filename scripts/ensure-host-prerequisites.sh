#!/usr/bin/env bash
# Bring an Amazon Linux 2023 aarch64 host up to the pinned toolchain.
#
#   scripts/ensure-host-prerequisites.sh [--dry-run] [--lock PATH]
#
# IDEMPOTENT BY CONSTRUCTION. Every action is guarded by a check, so the normal
# case -- a host that is already correct -- performs no installation at all.
# That matters because this runs on every deployment: a script that reinstalls
# or upgrades packages each time turns "deploy a reviewed commit" into "also
# apply whatever the mirrors changed since yesterday", which is unreviewed
# change arriving through the back door.
#
# What it deliberately never does:
#   * never formats, partitions or mounts a block device -- a deployment that
#     can reformat a data volume is one bug away from destroying every
#     transcript on the host;
#   * never uses --allowerasing, --skip-broken, --nogpgcheck or --disablerepo;
#   * never installs full `curl`, because on AL2023 that erases curl-minimal;
#   * never upgrades a package it did not install;
#   * never adds the radio account to the docker group -- membership there is
#     equivalent to root, and the runtime user must not have it;
#   * never opens a Docker TCP listener;
#   * never runs `docker system prune`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

LOCK_FILE="${RADIO_TOOLCHAIN_LOCK:-${SCRIPT_DIR}/../deploy/toolchain.lock.json}"
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
SKIP_MOUNT_CHECK="${RADIO_SKIP_MOUNT_CHECK:-0}"
DRY_RUN=0

INSTALLED=()
ALREADY_PRESENT=()

usage() {
    cat <<'USAGE'
Ensure the pinned host toolchain is present. Installs only what is missing.

Usage:
  scripts/ensure-host-prerequisites.sh [options]

Options:
  --lock PATH   Toolchain lock (default deploy/toolchain.lock.json).
  --dry-run     Report what is missing; install nothing.
  -h, --help    Show this help.

Never formats or mounts a disk. Never erases a package. Never upgrades a
package it did not install.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --lock)    LOCK_FILE="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit "${EXIT_OK}" ;;
        *)         usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

require_commands python3
[ -f "${LOCK_FILE}" ] || die "${EXIT_PRECONDITION}" "toolchain lock not found: ${LOCK_FILE}"

# lock_query <python-expression-over-`d`>
lock_query() {
    python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    d = json.load(handle)
print(eval(sys.argv[2], {"d": d}))  # noqa: S307 - fixed expressions from this file
' "${LOCK_FILE}" "$1"
}

# ---------------------------------------------------------------------------
stage "1/5  Validating the host"
ARCH="$(uname -m)"
[ "${ARCH}" = "aarch64" ] || die "${EXIT_PRECONDITION}" \
    "this toolchain is pinned for aarch64; host reports ${ARCH}"
log "architecture ${ARCH}"

# The data volume must be a real mount BEFORE anything is installed. Docker's
# data-root lives on it, and starting Docker with the volume unmounted would
# silently fill the root filesystem with images.
if [ "${SKIP_MOUNT_CHECK}" != "1" ]; then
    require_mountpoint "${DATA_ROOT}"
    log "${DATA_ROOT} is a mount point"
else
    warn "mount check skipped (RADIO_SKIP_MOUNT_CHECK=1); non-production validation only"
fi

# version_at_least <have> <want> -- dotted numeric comparison.
#
# Compared field by field as integers, never as strings: "3.3.4624.0" sorts
# BEFORE "3.3.2746.0" lexically, so a string comparison would reject the newer
# agent that is actually running in production.
version_at_least() {
    local have="$1" want="$2" i
    local -a h w
    IFS='.' read -r -a h <<<"${have}"
    IFS='.' read -r -a w <<<"${want}"
    for i in 0 1 2 3; do
        local hv="${h[i]:-0}" wv="${w[i]:-0}"
        case "${hv}${wv}" in
            *[!0-9]*) return 2 ;;   # malformed: caller decides
        esac
        if [ "${hv}" -gt "${wv}" ]; then return 0; fi
        if [ "${hv}" -lt "${wv}" ]; then return 1; fi
    done
    return 0
}

# ---------------------------------------------------------------------------
stage "2a/5 Checking the SSM Agent"
MIN_SSM_AGENT="$(lock_query 'd["minimum_versions"]["ssm_agent"]')"
SSM_AGENT_VERSION="${RADIO_SSM_AGENT_VERSION:-}"
if [ -z "${SSM_AGENT_VERSION}" ]; then
    SSM_AGENT_VERSION="$(rpm -q --queryformat '%{VERSION}' amazon-ssm-agent 2>/dev/null || true)"
fi
if [ -z "${SSM_AGENT_VERSION}" ]; then
    warn "amazon-ssm-agent version could not be determined; skipping the version gate"
    warn "this host is not managed by Systems Manager, or the agent is not an rpm"
else
    log "amazon-ssm-agent ${SSM_AGENT_VERSION} (minimum ${MIN_SSM_AGENT})"
    # A malformed version is a refusal, not a shrug: the deployment document
    # relies on ENV_VAR parameter interpolation, and an agent that does not
    # support it silently falls back to substituting the parameter into the
    # command text -- the exact behaviour the document is written to avoid.
    if ! version_at_least "${SSM_AGENT_VERSION}" "${MIN_SSM_AGENT}"; then
        case $? in
            2)
                fail "amazon-ssm-agent version '${SSM_AGENT_VERSION}' is not a dotted numeric version"
                ;;
            *)
                fail "amazon-ssm-agent ${SSM_AGENT_VERSION} is older than the required ${MIN_SSM_AGENT}"
                fail "older agents ignore interpolationType: ENV_VAR and fall back to raw string substitution"
                ;;
        esac
        remediation "sudo dnf update amazon-ssm-agent && sudo systemctl restart amazon-ssm-agent"
        die "${EXIT_PRECONDITION}" "unsupported SSM Agent version"
    fi
fi

# ---------------------------------------------------------------------------
stage "2/5  Checking packages"
REQUIRED_PACKAGES="$(lock_query 'chr(32).join(d["packages"]["required"])')"
FORBIDDEN_PACKAGES="$(lock_query 'chr(32).join(d["packages"]["forbidden"])')"

MISSING=()
for package in ${REQUIRED_PACKAGES}; do
    if rpm -q "${package}" >/dev/null 2>&1; then
        ALREADY_PRESENT+=("${package}")
    else
        MISSING+=("${package}")
    fi
done
log "${#ALREADY_PRESENT[@]} package(s) already present"

for package in ${FORBIDDEN_PACKAGES}; do
    for candidate in "${MISSING[@]}"; do
        [ "${candidate}" = "${package}" ] && die "${EXIT_PRECONDITION}" \
            "${package} is on the forbidden list; installing it would erase curl-minimal"
    done
done

if [ "${#MISSING[@]}" -eq 0 ]; then
    log "no package needs installing"
elif [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: would install ${MISSING[*]}"
else
    log "installing ${#MISSING[@]} missing package(s): ${MISSING[*]}"
    # Named packages only, from the enabled official repositories, with GPG
    # checking left on. No --allowerasing, no --skip-broken, no --nogpgcheck.
    dnf install -y "${MISSING[@]}" \
        || die "${EXIT_PRECONDITION}" "package installation failed"
    for package in "${MISSING[@]}"; do
        rpm -q "${package}" >/dev/null 2>&1 \
            || die "${EXIT_PRECONDITION}" "${package} still absent after installation"
        INSTALLED+=("${package}")
    done
    log "installed: ${INSTALLED[*]}"
fi

# ---------------------------------------------------------------------------
stage "3/5  Checking the Docker daemon"
DOCKER_CONFIG="$(lock_query 'd["docker_daemon"]["config_path"]')"
EXPECTED_DATA_ROOT="$(lock_query 'd["docker_daemon"]["expected"]["data-root"]')"

if [ ! -f "${DOCKER_CONFIG}" ]; then
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "dry run: would create ${DOCKER_CONFIG}"
    else
        log "creating ${DOCKER_CONFIG}"
        mkdir -p "$(dirname "${DOCKER_CONFIG}")"
        python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    expected = json.load(handle)["docker_daemon"]["expected"]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(expected, handle, indent=2, sort_keys=True)
    handle.write("\n")
' "${LOCK_FILE}" "${DOCKER_CONFIG}"
        chmod 0644 "${DOCKER_CONFIG}"
    fi
else
    # An existing configuration is never edited. Anything that disagrees with
    # the lock is reported: quietly rewriting data-root would orphan every
    # existing image and container, and quietly rewriting the log driver would
    # change how every container's logs are stored for no deployment reason.
    DAEMON_DIFF="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        actual = json.load(handle)
except Exception as error:
    print(f"unreadable: {type(error).__name__}")
    raise SystemExit(0)
with open(sys.argv[2], encoding="utf-8") as handle:
    expected = json.load(handle)["docker_daemon"]["expected"]

problems = []
for key, want in expected.items():
    have = actual.get(key)
    if have != want:
        problems.append(f"{key}: have {have!r}, expected {want!r}")

# A TCP listener exposes the Docker API, which is root on the host. It is a
# refusal regardless of what else matches.
for entry in actual.get("hosts", []) or []:
    if str(entry).startswith("tcp://"):
        problems.append(f"hosts: unexpected TCP listener {entry!r}")

print("; ".join(problems))
' "${DOCKER_CONFIG}" "${LOCK_FILE}")"
    if [ -n "${DAEMON_DIFF}" ]; then
        fail "${DOCKER_CONFIG} does not match the approved baseline: ${DAEMON_DIFF}"
        remediation "review ${DOCKER_CONFIG} by hand; this script never edits an existing daemon configuration"
        die "${EXIT_PRECONDITION}" "conflicting Docker daemon configuration"
    fi
    log "${DOCKER_CONFIG} matches the approved baseline"
fi

# Without this, Docker can start before the data volume is mounted and write its
# entire image store to the root filesystem, which then fills.
MOUNT_REQUIREMENT="$(lock_query 'd["docker_daemon"]["systemd_mount_requirement"]')"
if systemctl cat docker 2>/dev/null | grep -qF "${MOUNT_REQUIREMENT}"; then
    log "systemd unit carries ${MOUNT_REQUIREMENT}"
else
    warn "docker.service does not declare ${MOUNT_REQUIREMENT}"
    remediation "sudo systemctl edit docker  # add [Unit] ${MOUNT_REQUIREMENT}"
fi

# require_usable_image_store <data root>
#
# A CONFIGURED root is not a USABLE one, and `docker info` cannot tell them
# apart. Docker can start before the data volume is mounted -- exactly what the
# RequiresMountsFor above exists to prevent -- and the daemon then holds a
# storage driver initialised on the ROOT filesystem while the path resolves to
# the volume mounted over it. Everything reports correctly right up until a
# build fails with:
#
#   failed to prepare ... : symlink ../<id>/diff
#   /var/lib/radio/docker/overlay2/l/<link>: no such file or directory
#
# The overlay2 driver creates its `l` link directory when it initialises,
# whether or not any image exists, so a missing `l` means the store the daemon
# is using is not the one at this path. Restarting is the fix, and it is safe
# only when nothing is running -- so that is the condition.
require_usable_image_store() {
    local root="$1" driver running attempt
    driver="$(docker info --format '{{.Driver}}' 2>/dev/null || echo '')"
    if [ "${driver}" != "overlay2" ]; then
        log "storage driver ${driver:-unknown}; image store layout not checked"
        return 0
    fi
    if [ -d "${root}/overlay2/l" ]; then
        log "image store initialised at ${root} (driver overlay2)"
        return 0
    fi

    warn "${root}/overlay2/l is missing: the daemon's image store is not on the filesystem now mounted here"
    # Fail closed: if the daemon will not say what is running, the one thing we
    # must not do is restart it and find out.
    if ! running="$(docker ps --quiet 2>/dev/null | wc -l | tr -d ' ')"; then
        die "${EXIT_PRECONDITION}" \
            "cannot list running containers; refusing to restart Docker without knowing what it would stop"
    fi
    if [ "${running}" != "0" ]; then
        # Never trade someone's running service for a build.
        fail "${running} container(s) are running; restarting Docker would stop them"
        remediation "stop the running containers, then: sudo systemctl restart docker"
        die "${EXIT_PRECONDITION}" \
            "Docker must be restarted to see its data volume, which is not safe while containers are running"
    fi

    log "no containers are running; restarting docker so it re-initialises against ${root}"
    systemctl restart docker \
        || die "${EXIT_PRECONDITION}" "could not restart docker"
    for attempt in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 1
    done
    docker info >/dev/null 2>&1 \
        || die "${EXIT_PRECONDITION}" "docker did not come back after being restarted"
    if [ ! -d "${root}/overlay2/l" ]; then
        fail "${root}/overlay2/l is still missing after a restart"
        remediation "confirm the data volume is mounted at /var/lib/radio and that ${root} is writable"
        die "${EXIT_PRECONDITION}" "the Docker image store is unusable"
    fi
    log "docker restarted; image store now initialised at ${root}"
}

if [ "${DRY_RUN}" -eq 0 ]; then
    if ! systemctl is-enabled docker >/dev/null 2>&1; then
        log "enabling docker"
        systemctl enable docker
    fi
    if ! systemctl is-active docker >/dev/null 2>&1; then
        log "starting docker"
        systemctl start docker
    fi
    require_commands docker
    RUNTIME_DATA_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo '')"
    if [ "${RUNTIME_DATA_ROOT}" != "${EXPECTED_DATA_ROOT}" ]; then
        die "${EXIT_PRECONDITION}" \
            "Docker is running with root dir '${RUNTIME_DATA_ROOT}', expected '${EXPECTED_DATA_ROOT}'"
    fi
    log "docker active with root dir ${RUNTIME_DATA_ROOT}"
    require_usable_image_store "${RUNTIME_DATA_ROOT}"
else
    log "dry run: not starting or inspecting the docker service"
fi

# ---------------------------------------------------------------------------
# install_verified_plugin <label> <url> <sha256> <destination>
#
# Download, verify, then install. The digest is checked BEFORE the file is
# given a name anything would execute, and a mismatch removes the download
# rather than leaving it somewhere hopeful.
install_verified_plugin() {
    local label="$1" url="$2" expected="$3" destination="$4"
    local temp actual
    require_commands curl sha256sum
    temp="$(mktemp)"
    log "downloading ${label}"
    curl -fsSL --proto '=https' --tlsv1.2 -o "${temp}" "${url}" \
        || { rm -f "${temp}"; die "${EXIT_PRECONDITION}" "could not download ${url}"; }
    actual="$(sha256sum "${temp}" | awk '{print $1}')"
    if [ "${actual}" != "${expected}" ]; then
        rm -f "${temp}"
        die "${EXIT_PRECONDITION}" \
            "${label} checksum mismatch: expected ${expected}, got ${actual}"
    fi
    log "checksum verified"
    mkdir -p "$(dirname "${destination}")"
    chmod 0755 "${temp}"
    mv -f "${temp}" "${destination}"
    log "installed ${label}"
}

# ---------------------------------------------------------------------------
stage "4/5  Checking the Docker Compose plugin"
COMPOSE_VERSION="$(lock_query 'd["docker_compose"]["version"]')"
COMPOSE_ASSET="$(lock_query 'd["docker_compose"]["linux_aarch64"]["asset"]')"
COMPOSE_SHA="$(lock_query 'd["docker_compose"]["linux_aarch64"]["sha256"]')"
COMPOSE_PATH="$(lock_query 'd["docker_compose"]["linux_aarch64"]["install_path"]')"

compose_is_correct() {
    [ -x "${COMPOSE_PATH}" ] || return 1
    local actual
    actual="$(sha256sum "${COMPOSE_PATH}" 2>/dev/null | awk '{print $1}')"
    [ "${actual}" = "${COMPOSE_SHA}" ]
}

if compose_is_correct; then
    log "docker compose ${COMPOSE_VERSION} already installed and matches the pinned digest"
    ALREADY_PRESENT+=("docker-compose-plugin")
elif [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: would install docker compose ${COMPOSE_VERSION}"
else
    if [ -e "${COMPOSE_PATH}" ]; then
        # Present but wrong. Not overwritten silently: an operator installed
        # something here, and replacing it without a word hides whatever they
        # were doing -- and whatever went wrong.
        fail "${COMPOSE_PATH} exists but does not match the pinned ${COMPOSE_VERSION} digest"
        remediation "inspect ${COMPOSE_PATH}, then remove it explicitly to allow a pinned reinstall"
        die "${EXIT_PRECONDITION}" "unverifiable docker compose binary"
    fi
    install_verified_plugin "docker compose ${COMPOSE_VERSION}" \
        "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/${COMPOSE_ASSET}" \
        "${COMPOSE_SHA}" "${COMPOSE_PATH}"
    INSTALLED+=("docker-compose-plugin")
fi

# Installing the right file is not the same as the CLI using it. Docker searches
# several plugin directories, so a second copy elsewhere -- left by a package, or
# by an earlier manual install -- can win, and then an unrelated Docker upgrade
# changes which Compose runs with nothing having been deployed.
if [ "${DRY_RUN}" -eq 0 ] && command -v docker >/dev/null 2>&1; then
    ACTIVE_COMPOSE="$(docker compose version --short 2>/dev/null || true)"
    EXPECTED_SHORT="${COMPOSE_VERSION#v}"
    if [ -z "${ACTIVE_COMPOSE}" ]; then
        die "${EXIT_PRECONDITION}" "the docker CLI does not resolve a compose plugin"
    fi
    if [ "${ACTIVE_COMPOSE#v}" != "${EXPECTED_SHORT}" ]; then
        fail "docker compose resolves to ${ACTIVE_COMPOSE}, expected ${EXPECTED_SHORT}"
        remediation "find / -name docker-compose -path '*cli-plugins*' 2>/dev/null  # remove the duplicate"
        die "${EXIT_PRECONDITION}" "an unexpected compose plugin is active"
    fi
    log "docker compose resolves to ${ACTIVE_COMPOSE} from the pinned plugin"
fi

# ---------------------------------------------------------------------------
stage "4a/5 Checking the Docker Buildx plugin"
# Compose v5 has no legacy builder to fall back to. Without buildx it refuses:
# "compose build requires buildx 0.17.0 or later" -- which is where the first
# install failed, having passed every other gate. Installed the same way as
# Compose, into the same directory, so a buildx shipped by the docker package is
# superseded without being removed or modified.
BUILDX_VERSION="$(lock_query 'd["docker_buildx"]["version"]')"
BUILDX_ASSET="$(lock_query 'd["docker_buildx"]["linux_aarch64"]["asset"]')"
BUILDX_SHA="$(lock_query 'd["docker_buildx"]["linux_aarch64"]["sha256"]')"
BUILDX_PATH="$(lock_query 'd["docker_buildx"]["linux_aarch64"]["install_path"]')"
BUILDX_MINIMUM="$(lock_query 'd["docker_buildx"]["minimum_supported"]')"

buildx_is_correct() {
    [ -x "${BUILDX_PATH}" ] || return 1
    local actual
    actual="$(sha256sum "${BUILDX_PATH}" 2>/dev/null | awk '{print $1}')"
    [ "${actual}" = "${BUILDX_SHA}" ]
}

if buildx_is_correct; then
    log "docker buildx ${BUILDX_VERSION} already installed and matches the pinned digest"
    ALREADY_PRESENT+=("docker-buildx-plugin")
elif [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: would install docker buildx ${BUILDX_VERSION}"
else
    if [ -e "${BUILDX_PATH}" ]; then
        # Present but wrong. Same reasoning as Compose: someone installed this,
        # and replacing it silently hides both what they were doing and whatever
        # went wrong.
        fail "${BUILDX_PATH} exists but does not match the pinned ${BUILDX_VERSION} digest"
        remediation "inspect ${BUILDX_PATH}, then remove it explicitly to allow a pinned reinstall"
        die "${EXIT_PRECONDITION}" "unverifiable docker buildx binary"
    fi
    install_verified_plugin "docker buildx ${BUILDX_VERSION}" \
        "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/${BUILDX_ASSET}" \
        "${BUILDX_SHA}" "${BUILDX_PATH}"
    INSTALLED+=("docker-buildx-plugin")
fi

# Installing the right file is not the same as the CLI using it, and this is the
# check that would have caught the original failure before a build was attempted.
if [ "${DRY_RUN}" -eq 0 ] && command -v docker >/dev/null 2>&1; then
    ACTIVE_BUILDX="$(docker buildx version 2>/dev/null | awk '{print $2}' | tr -d 'v')"
    if [ -z "${ACTIVE_BUILDX}" ]; then
        die "${EXIT_PRECONDITION}" \
            "the docker CLI does not resolve a buildx plugin; compose build cannot run without one"
    fi
    if ! version_at_least "${ACTIVE_BUILDX}" "${BUILDX_MINIMUM}"; then
        fail "docker buildx resolves to ${ACTIVE_BUILDX}, below the ${BUILDX_MINIMUM} Compose requires"
        remediation "find / -name docker-buildx -path '*cli-plugins*' 2>/dev/null  # remove the older duplicate"
        die "${EXIT_PRECONDITION}" "the active buildx is too old to build"
    fi
    log "docker buildx resolves to ${ACTIVE_BUILDX} (minimum ${BUILDX_MINIMUM})"
fi

# ---------------------------------------------------------------------------
stage "5/5  Summary"
log "already present: ${#ALREADY_PRESENT[@]}"
log "installed now:   ${#INSTALLED[@]}${INSTALLED:+ (${INSTALLED[*]})}"
if [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: nothing was installed or started"
fi
exit "${EXIT_OK}"
