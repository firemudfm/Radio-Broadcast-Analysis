#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/firemud/radio-intelligence-api
CONFIG_FILE=/etc/firemud/radio-intelligence.env

if [[ ${EUID} -ne 0 ]]; then
  echo "[analysis-worker-install] Run with sudo" >&2
  exit 1
fi
if [[ ! -x "$APP_DIR/venv/bin/python" || ! -f "$CONFIG_FILE" ]]; then
  echo "[analysis-worker-install] Install or upgrade the FastAPI application first" >&2
  exit 1
fi
cp "$SOURCE_DIR/deploy/radio-analysis-worker.service" /etc/systemd/system/radio-analysis-worker.service
systemctl daemon-reload
systemctl enable radio-analysis-worker >/dev/null
systemctl restart radio-analysis-worker
sleep 2
systemctl is-active --quiet radio-analysis-worker

echo "[analysis-worker-install] Shared analysis worker is active"
