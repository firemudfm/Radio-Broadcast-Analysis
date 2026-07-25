#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR=/opt/firemud/llm-runtime
SRC_DIR="$RUNTIME_DIR/src/llama.cpp"
BUILD_DIR="$SRC_DIR/build"
BIN_DIR="$RUNTIME_DIR/bin"
MODEL_DIR="$RUNTIME_DIR/models"
CONFIG_DIR=/etc/firemud
CONFIG_FILE="$CONFIG_DIR/radio-llm.env"
MODEL_FILE="$MODEL_DIR/Qwen3-0.6B-Q8_0.gguf"
LLAMA_TAG="${LLAMA_CPP_TAG:-b10034}"
MODEL_URL="${QWEN_MODEL_URL:-https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q8_0.gguf?download=true}"

if [[ ${EUID} -ne 0 ]]; then
  echo "[llm-install] Run with sudo" >&2
  exit 1
fi
if ! grep -q '^ID="\?amzn"\?' /etc/os-release || ! grep -q '^VERSION_ID="\?2023"\?' /etc/os-release; then
  echo "[llm-install] Amazon Linux 2023 is required" >&2
  exit 1
fi
if ! id radio >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /sbin/nologin radio
fi

echo "[llm-install] Installing native build prerequisites"
dnf install -y ca-certificates cmake gcc-c++ git make curl-minimal jq

mkdir -p "$RUNTIME_DIR/src" "$BIN_DIR" "$MODEL_DIR" "$CONFIG_DIR"

if [[ ! -d "$SRC_DIR/.git" ]]; then
  echo "[llm-install] Cloning llama.cpp tag $LLAMA_TAG"
  git clone --depth 1 --branch "$LLAMA_TAG" https://github.com/ggml-org/llama.cpp.git "$SRC_DIR"
else
  echo "[llm-install] Reusing existing llama.cpp source"
fi

echo "[llm-install] Building CPU llama-server"
cmake -S "$SRC_DIR" -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=ON \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DBUILD_SHARED_LIBS=OFF
cmake --build "$BUILD_DIR" --config Release -j "$(nproc)" --target llama-server
install -m 0750 "$BUILD_DIR/bin/llama-server" "$BIN_DIR/llama-server"

if [[ ! -s "$MODEL_FILE" || $(stat -c '%s' "$MODEL_FILE") -lt 500000000 ]]; then
  echo "[llm-install] Downloading official Qwen3-0.6B Q8 GGUF (~639 MB)"
  tmp="$MODEL_FILE.part"
  rm -f "$tmp"
  curl -fL --retry 5 --retry-delay 3 "$MODEL_URL" -o "$tmp"
  if [[ $(stat -c '%s' "$tmp") -lt 500000000 ]]; then
    echo "[llm-install] Downloaded model is unexpectedly small" >&2
    exit 1
  fi
  mv "$tmp" "$MODEL_FILE"
else
  echo "[llm-install] Reusing existing model: $MODEL_FILE"
fi
sha256sum "$MODEL_FILE" > "$MODEL_FILE.sha256"

if [[ ! -f "$CONFIG_FILE" ]]; then
  cp "$SOURCE_DIR/deploy/radio-llm.env.example" "$CONFIG_FILE"
fi
cp "$SOURCE_DIR/deploy/radio-llm.service" /etc/systemd/system/radio-llm.service

chown -R root:radio "$RUNTIME_DIR" "$CONFIG_FILE"
chmod 0750 "$RUNTIME_DIR" "$BIN_DIR" "$MODEL_DIR"
chmod 0640 "$MODEL_FILE" "$MODEL_FILE.sha256" "$CONFIG_FILE"

systemctl daemon-reload
systemctl enable radio-llm >/dev/null
systemctl restart radio-llm

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a
for attempt in $(seq 1 60); do
  if curl -fsS "http://${RADIO_LLM_HOST}:${RADIO_LLM_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  if [[ $attempt -eq 60 ]]; then
    echo "[llm-install] LLM health check failed" >&2
    journalctl -u radio-llm -n 100 --no-pager >&2 || true
    exit 1
  fi
  sleep 2
done

response="$(curl -fsS \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-0.6b-q8","messages":[{"role":"user","content":"/no_think Return only the word READY"}],"max_tokens":12,"temperature":0}' \
  "http://${RADIO_LLM_HOST}:${RADIO_LLM_PORT}/v1/chat/completions")"
echo "$response" | jq -e '.choices[0].message.content | type == "string"' >/dev/null

echo "[llm-install] Local multilingual LLM is ready"
echo "[llm-install] Model: $MODEL_FILE"
echo "[llm-install] Endpoint: http://${RADIO_LLM_HOST}:${RADIO_LLM_PORT}"
