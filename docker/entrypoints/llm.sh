#!/bin/sh
# llama-server entrypoint.
#
# Fails fast when the model is missing. A server that starts without one would
# pass a TCP health check and then fail every analysis request -- the analysis
# worker would fall back correctly, but every mention would silently lose its
# summary, which is a much worse failure than not starting.
set -eu

: "${RADIO_LLM_MODEL_PATH:=/models/qwen/Qwen3-0.6B-Q8_0.gguf}"
: "${RADIO_LLM_PORT:=8790}"
: "${RADIO_LLM_THREADS:=2}"
: "${RADIO_LLM_CONTEXT:=4096}"

if [ ! -r "${RADIO_LLM_MODEL_PATH}" ]; then
    # Guidance is HOST-side on purpose. This container has no Python, no
    # network egress for model fetching, and only a read-only /models mount --
    # downloading from here is neither possible nor desirable. Acquisition is
    # an explicit operator step against models.lock.json.
    echo "llm.sh: model not readable at ${RADIO_LLM_MODEL_PATH}" >&2
    echo "" >&2
    echo "The model is never downloaded automatically. On the HOST, from the" >&2
    echo "repository root, run:" >&2
    echo "" >&2
    echo "  python3 scripts/download-models.py \\" >&2
    echo "    --root /var/lib/radio/models \\" >&2
    echo "    --role llm" >&2
    echo "" >&2
    echo "Then verify before starting the stack:" >&2
    echo "" >&2
    echo "  python3 scripts/verify-models.py \\" >&2
    echo "    --root /var/lib/radio/models \\" >&2
    echo "    --role llm" >&2
    echo "" >&2
    echo "See docs/MODEL_MANAGEMENT.md" >&2
    exit 78
fi

echo "llama.cpp $(cat /etc/llama-cpp-commit 2>/dev/null || echo unknown)" >&2

# --host 0.0.0.0 binds inside the container only; compose.yaml never publishes
# this port, so the socket is reachable on the Compose network and nowhere else.
#
# --jinja renders the model's own chat template, which is what makes the
# `enable_thinking=false` switch effective for Qwen3 (research doc, section 1.8).
exec llama-server \
    --model "${RADIO_LLM_MODEL_PATH}" \
    --host 0.0.0.0 \
    --port "${RADIO_LLM_PORT}" \
    --threads "${RADIO_LLM_THREADS}" \
    --ctx-size "${RADIO_LLM_CONTEXT}" \
    --jinja \
    --no-warmup \
    --metrics
