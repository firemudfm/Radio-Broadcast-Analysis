#!/usr/bin/env bash
# Measure how many DISTINCT live stations this host can actually decode.
#
#   sudo scripts/benchmark-capacity.sh --stations 2 [--minutes 30] [--dry-run]
#
# NOT run by CI, and not run by the deployment. It needs real streams, real
# models and a host that is not serving traffic, so it is an operator action.
#
# It exists because RADIO_MAX_ACTIVE_UNIQUE_STATIONS defaults to 1 and there is
# no measurement justifying anything higher. Raising that number without running
# this is how the spool fills and audio is lost silently: ASR falls behind real
# time, the queue ages, and nothing surfaces until the disk watermark trips.
#
# This script MEASURES. It changes no configuration and starts no campaign.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

STATIONS=1
MINUTES=30
DRY_RUN=0
DATA_ROOT="${RADIO_DATA_ROOT:-/var/lib/radio}"
OUT_DIR="${RADIO_BENCHMARK_DIR:-${DATA_ROOT}/logs/benchmarks}"
SAMPLE_SECONDS="${RADIO_BENCHMARK_SAMPLE_SECONDS:-30}"

usage() {
    cat <<'USAGE'
Measure real decode and ASR capacity at a given active-station count.

Usage:
  sudo scripts/benchmark-capacity.sh --stations N [options]

Required:
  --stations N     Distinct active stations to measure (1, 2, 5 or 8).

Options:
  --minutes N      Sustained duration (default 30). Short runs miss the
                   backlog trend, which is the whole signal.
  --dry-run        Show what would be sampled; measure nothing.
  -h, --help       Show this help.

Records connection success, reconnects, ffmpeg and worker CPU, ASR real-time
factor, queue age and backlog, dropped segments, ring-buffer overruns, memory,
swap, OOM kills, spool growth, SQLite busy retries and Qwen latency.

Changes no configuration. See docs/CAPACITY.md for the stop conditions that
must ALL hold before RADIO_MAX_ACTIVE_UNIQUE_STATIONS is raised.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stations) STATIONS="${2:-}"; shift 2 ;;
        --minutes)  MINUTES="${2:-}"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)  usage; exit "${EXIT_OK}" ;;
        *)          usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

case "${STATIONS}" in
    ''|*[!0-9]*) die "${EXIT_USAGE}" "--stations must be a positive integer" ;;
esac
[ "${STATIONS}" -ge 1 ] || die "${EXIT_USAGE}" "--stations must be at least 1"
case "${MINUTES}" in
    ''|*[!0-9]*) die "${EXIT_USAGE}" "--minutes must be a positive integer" ;;
esac

require_commands docker python3 date awk

stage "Benchmark plan"
log "active stations under test: ${STATIONS}"
log "duration: ${MINUTES} minutes, sampled every ${SAMPLE_SECONDS}s"
log "this script changes no configuration and starts no campaign"

if [ "${DRY_RUN}" -eq 1 ]; then
    stage "Dry run"
    log "would sample: container CPU, ASR real-time factor, queue age and depth,"
    log "              spool usage, memory, swap, OOM kills, SQLite busy retries"
    log "would write:  ${OUT_DIR}/benchmark-<stations>-<timestamp>.jsonl"
    exit "${EXIT_OK}"
fi

# Refuse to measure something that is not the thing being measured.
stage "Verifying the host is in the state being benchmarked"
ACTIVE_LIMIT="$(python3 -c '
import os, sys
path = "/etc/radio-broadcast-analysis/application.env"
try:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            name, _, value = line.strip().partition("=")
            if name == "RADIO_MAX_ACTIVE_UNIQUE_STATIONS":
                print(value.strip())
                sys.exit(0)
except OSError:
    pass
print("")
')"
if [ -n "${ACTIVE_LIMIT}" ] && [ "${ACTIVE_LIMIT}" -lt "${STATIONS}" ]; then
    fail "the host is configured for ${ACTIVE_LIMIT} active station(s), not ${STATIONS}"
    fail "it cannot decode ${STATIONS} at once, so the result would describe nothing"
    remediation "raise RADIO_MAX_ACTIVE_UNIQUE_STATIONS to ${STATIONS} on a NON-production host, or benchmark ${ACTIVE_LIMIT}"
    die "${EXIT_PRECONDITION}" "configured capacity is below the benchmark target"
fi

install -d -m 0750 "${OUT_DIR}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="${OUT_DIR}/benchmark-${STATIONS}-${STAMP}.jsonl"
install -m 0640 /dev/null "${REPORT}"

stage "Sampling"
log "writing ${REPORT}"

DEADLINE=$(( $(date +%s) + MINUTES * 60 ))
SAMPLE=0
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
    SAMPLE=$(( SAMPLE + 1 ))

    # One JSON object per sample. Container stats are read without --no-stream
    # per container, which would serialise; one call covers them all.
    docker stats --no-stream --format \
        '{"container":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}"}' \
        2>/dev/null \
        | python3 -c '
import json, os, subprocess, sys, time

containers = []
for line in sys.stdin:
    line = line.strip()
    if line:
        try:
            containers.append(json.loads(line))
        except ValueError:
            pass


def read_meminfo():
    values = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                values[key] = int(rest.split()[0])
    except OSError:
        pass
    return values


meminfo = read_meminfo()
spool = os.environ.get("SPOOL_PATH", "/var/lib/radio/spool")
try:
    usage = subprocess.run(  # noqa: S603
        ["df", "-Pm", spool], capture_output=True, text=True, check=False, timeout=10
    ).stdout.splitlines()[1].split()
    spool_used_mib, spool_free_mib = int(usage[2]), int(usage[3])
except Exception:
    spool_used_mib = spool_free_mib = None

# OOM kills since boot: the single most decisive signal. A host that OOMs at
# this station count does not support this station count, whatever the averages.
oom_kills = 0
try:
    with open("/proc/vmstat", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("oom_kill"):
                oom_kills = int(line.split()[1])
except OSError:
    pass

print(json.dumps({
    "sample": int(os.environ.get("SAMPLE", "0")),
    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "stations_under_test": int(os.environ.get("STATIONS", "0")),
    "containers": containers,
    "memory_available_mib": meminfo.get("MemAvailable", 0) // 1024,
    "swap_used_mib": (meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0)) // 1024,
    "oom_kills_since_boot": oom_kills,
    "spool_used_mib": spool_used_mib,
    "spool_free_mib": spool_free_mib,
}))
' >> "${REPORT}" || warn "sample ${SAMPLE} failed"

    sleep "${SAMPLE_SECONDS}"
done

stage "Benchmark complete"
log "samples: ${SAMPLE}"
log "report:  ${REPORT}"
log ""
log "This report does NOT by itself justify raising the active limit."
log "Check every stop condition in docs/CAPACITY.md first:"
log "  ASR real-time factor < 0.8, queue age stable, zero dropped segments,"
log "  zero ring-buffer overruns, spool below warning, no swap, no OOM kills."
log "If any condition fails at ${STATIONS} stations, the supported limit is $(( STATIONS - 1 ))."
exit "${EXIT_OK}"
