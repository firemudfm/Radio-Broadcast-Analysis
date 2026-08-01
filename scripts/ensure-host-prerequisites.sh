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
    # An existing configuration is never edited. A data-root that disagrees with
    # the lock means images live somewhere this deployment does not manage, and
    # quietly rewriting it would orphan every existing image and container.
    ACTUAL_DATA_ROOT="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.load(handle).get("data-root", ""))
except Exception:
    print("<unreadable>")
' "${DOCKER_CONFIG}")"
    if [ "${ACTUAL_DATA_ROOT}" != "${EXPECTED_DATA_ROOT}" ]; then
        fail "${DOCKER_CONFIG} has data-root '${ACTUAL_DATA_ROOT}', expected '${EXPECTED_DATA_ROOT}'"
        remediation "review ${DOCKER_CONFIG} by hand; this script never edits an existing daemon configuration"
        die "${EXIT_PRECONDITION}" "conflicting Docker daemon configuration"
    fi
    log "${DOCKER_CONFIG} already matches the expected data-root"
fi

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
else
    log "dry run: not starting or inspecting the docker service"
fi

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
    require_commands curl sha256sum
    url="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/${COMPOSE_ASSET}"
    temp="$(mktemp)"
    log "downloading docker compose ${COMPOSE_VERSION}"
    curl -fsSL --proto '=https' --tlsv1.2 -o "${temp}" "${url}" \
        || { rm -f "${temp}"; die "${EXIT_PRECONDITION}" "could not download ${url}"; }
    actual="$(sha256sum "${temp}" | awk '{print $1}')"
    if [ "${actual}" != "${COMPOSE_SHA}" ]; then
        rm -f "${temp}"
        die "${EXIT_PRECONDITION}" \
            "docker compose checksum mismatch: expected ${COMPOSE_SHA}, got ${actual}"
    fi
    log "checksum verified"
    mkdir -p "$(dirname "${COMPOSE_PATH}")"
    chmod 0755 "${temp}"
    mv -f "${temp}" "${COMPOSE_PATH}"
    INSTALLED+=("docker-compose-plugin")
    log "installed docker compose ${COMPOSE_VERSION}"
fi

# ---------------------------------------------------------------------------
stage "5/5  Summary"
log "already present: ${#ALREADY_PRESENT[@]}"
log "installed now:   ${#INSTALLED[@]}${INSTALLED:+ (${INSTALLED[*]})}"
if [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: nothing was installed or started"
fi
exit "${EXIT_OK}"
