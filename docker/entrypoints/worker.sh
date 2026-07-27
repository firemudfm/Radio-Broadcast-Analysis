#!/bin/sh
# Pipeline worker entrypoint.
#
# One script for all five roles. The role is the first argument and is matched
# against an explicit allowlist -- never interpolated into a module path -- so a
# malformed value cannot be turned into "import whatever you like".
#
# `exec` is load-bearing: the Python process must become PID 1's direct child so
# Docker's SIGTERM reaches it. A shell that stays in the middle swallows the
# signal, the worker never shuts down cleanly, and Compose SIGKILLs it after the
# grace period -- losing the in-flight segment or open conversation the graceful
# path exists to protect.
set -eu

ROLE="${1:-${RADIO_WORKER_ROLE:-}}"

case "${ROLE}" in
    planner|listener|transcription|analysis|cleanup)
        ;;
    "")
        echo "worker.sh: no role given." >&2
        echo "Usage: worker.sh {planner|listener|transcription|analysis|cleanup}" >&2
        exit 64
        ;;
    *)
        echo "worker.sh: unknown role '${ROLE}'." >&2
        echo "Valid roles: planner listener transcription analysis cleanup" >&2
        exit 64
        ;;
esac

echo "Starting ${ROLE} worker (pipeline_mode=${RADIO_PIPELINE_MODE:-unset})" >&2

exec python -m "app.workers.${ROLE}"
