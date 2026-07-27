#!/usr/bin/env bash
# Validate the Compose stack and audit its security posture.
#
# Runs `docker compose config` (which resolves anchors, overlays and variable
# substitution the way a real `up` would) and then asserts the properties that
# must hold. Reviewing the source files by eye is not equivalent: an overlay can
# silently reintroduce something the base forbade, and only the resolved output
# shows what would actually run.
#
#   scripts/compose-check.sh              # production overlay
#   scripts/compose-check.sh dev          # development overlay
set -euo pipefail

cd "$(dirname "$0")/.."

OVERLAY="${1:-prod}"
case "${OVERLAY}" in
    prod) COMPOSE_FILES=(-f compose.yaml -f compose.prod.yaml) ;;
    dev)  COMPOSE_FILES=(-f compose.yaml -f compose.dev.yaml) ;;
    *)    echo "usage: $0 [prod|dev]" >&2; exit 64 ;;
esac

# Point env_file at the committed dev values so validation needs no /etc access
# and no real credentials. The production default is unchanged.
export RADIO_ENV_DIR="${RADIO_ENV_DIR:-./deploy/dev}"

RESOLVED="$(mktemp)"
trap 'rm -f "${RESOLVED}"' EXIT

echo "==> Resolving ${OVERLAY} configuration"
docker compose "${COMPOSE_FILES[@]}" \
    --profile core --profile pipeline --profile llm \
    config > "${RESOLVED}"
echo "    syntax OK"

echo "==> Auditing the resolved configuration"
python3 - "${RESOLVED}" <<'PYTHON'
import re
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    document = yaml.safe_load(handle)

services = document.get("services", {})
problems = []
notes = []


def mebibytes(value):
    text = str(value)
    match = re.match(r"^(\d+)\s*([kKmMgG]?)i?[bB]?$", text)
    if not match:
        return int(text) // 1048576
    amount, unit = int(match.group(1)), match.group(2).lower()
    if unit == "g":
        return amount * 1024
    if unit == "m":
        return amount
    if unit == "k":
        return amount // 1024
    return amount // 1048576


total_memory = 0
for name, service in sorted(services.items()):
    # --- forbidden outright --------------------------------------------------
    if service.get("privileged"):
        problems.append(f"{name}: privileged")
    if service.get("network_mode") == "host":
        problems.append(f"{name}: host networking")
    if service.get("cap_add"):
        problems.append(f"{name}: adds capabilities {service['cap_add']}")
    for volume in service.get("volumes", []):
        source = volume.get("source") if isinstance(volume, dict) else str(volume)
        if "docker.sock" in str(source):
            problems.append(f"{name}: mounts the Docker socket")

    # --- required ------------------------------------------------------------
    if service.get("cap_drop") != ["ALL"]:
        problems.append(f"{name}: cap_drop is {service.get('cap_drop')}, expected [ALL]")
    if "no-new-privileges:true" not in (service.get("security_opt") or []):
        problems.append(f"{name}: missing no-new-privileges")
    if not service.get("init"):
        problems.append(f"{name}: init is not enabled (zombie reaping)")

    # --- secrets -------------------------------------------------------------
    # Only credential SHAPES are checked here. Whether a value came from an
    # env_file or was written inline is NOT distinguishable in resolved output
    # (compose merges env_file into environment), so inline-secret detection is
    # done against the source files further down, where it can be accurate.
    for key, value in (service.get("environment") or {}).items():
        text = str(value or "")
        if re.search(r"(AKIA|ASIA)[0-9A-Z]{16}", text):
            problems.append(f"{name}: an AWS access key id appears in {key}")
        if re.match(r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/\d{12}/", text):
            notes.append(f"{name}: {key} carries a queue URL (expected)")

    # --- exposure ------------------------------------------------------------
    for port in service.get("ports", []) or []:
        host_ip = port.get("host_ip") or "0.0.0.0"
        published = port.get("published")
        if name != "api":
            problems.append(f"{name}: publishes {published}; only the api may publish")
        if str(published) == "8790":
            problems.append(f"{name}: publishes the LLM port to the host")
        notes.append(f"{name}: published {host_ip}:{published} -> {port.get('target')}")

    limits = (service.get("deploy") or {}).get("resources", {}).get("limits", {})
    if limits.get("memory"):
        total_memory += mebibytes(limits["memory"])

if "llm" in services and services["llm"].get("ports"):
    problems.append("llm: must not publish any port to the host")

for note in notes:
    print(f"    note: {note}")

if total_memory:
    print(f"    total memory limits: {total_memory} MiB")
    if total_memory > 7168:
        problems.append(
            f"memory limits total {total_memory} MiB, leaving too little of an 8 GiB host"
        )

if problems:
    print("\nFAILED:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    sys.exit(1)

print("    security posture OK")
PYTHON

echo "==> Checking the Compose sources for inline secrets"
# Accurate here, unlike in resolved output: anything matching in the committed
# files really was written inline.
if grep -nE '(AKIA|ASIA)[0-9A-Z]{16}|aws_secret_access_key[[:space:]]*[:=]'         compose.yaml compose.dev.yaml compose.prod.yaml; then
    echo "  - a credential appears in a committed Compose file" >&2
    exit 1
fi
if grep -nE '^[[:space:]]+RADIO_AUDIO_TOKEN_SECRET:[[:space:]]*[^$#]'         compose.yaml compose.dev.yaml compose.prod.yaml; then
    echo "  - RADIO_AUDIO_TOKEN_SECRET is set inline; use an env file" >&2
    exit 1
fi
echo "    no inline secrets"

echo "==> Checking that no secret files would enter the build context"
for forbidden in .env application.env infrastructure.env; do
    if git check-ignore -q "${forbidden}" 2>/dev/null; then
        continue
    fi
    if [ -e "${forbidden}" ]; then
        echo "  - ${forbidden} exists and is not git-ignored" >&2
        exit 1
    fi
done
echo "    build context OK"

echo
echo "compose-check (${OVERLAY}): PASS"
