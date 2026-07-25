#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/firemud/radio-intelligence-api
CONFIG_FILE=/etc/firemud/radio-intelligence.env
SERVICE=radio-intelligence-api
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/opt/firemud/backups/${SERVICE}-${STAMP}"

if [[ ${EUID} -ne 0 ]]; then
  echo "[radio-api-upgrade] Run with sudo" >&2
  exit 1
fi
if ! grep -q '^ID="\?amzn"\?' /etc/os-release || ! grep -q '^VERSION_ID="\?2023"\?' /etc/os-release; then
  echo "[radio-api-upgrade] Amazon Linux 2023 is required" >&2
  exit 1
fi
if [[ ! -x "$APP_DIR/venv/bin/python" || ! -d "$APP_DIR/app" ]]; then
  echo "[radio-api-upgrade] Existing API installation was not found at $APP_DIR" >&2
  echo "Run deploy/install-amazon-linux.sh for a first installation." >&2
  exit 1
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[radio-api-upgrade] Configuration is missing: $CONFIG_FILE" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
cp -a "$APP_DIR/app" "$BACKUP_DIR/app"
cp -a "$APP_DIR/VERSION" "$BACKUP_DIR/VERSION" 2>/dev/null || true
cp -a "$APP_DIR/requirements.txt" "$BACKUP_DIR/requirements.txt" 2>/dev/null || true
cp -a /etc/systemd/system/radio-intelligence-api.service "$BACKUP_DIR/radio-intelligence-api.service" 2>/dev/null || true

echo "[radio-api-upgrade] Backup: $BACKUP_DIR"
was_active=false
if systemctl is-active --quiet "$SERVICE"; then
  was_active=true
  systemctl stop "$SERVICE"
fi

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "[radio-api-upgrade] Upgrade failed; restoring previous application" >&2
    rm -rf "$APP_DIR/app"
    cp -a "$BACKUP_DIR/app" "$APP_DIR/app"
    [[ -f "$BACKUP_DIR/VERSION" ]] && cp -a "$BACKUP_DIR/VERSION" "$APP_DIR/VERSION"
    [[ -f "$BACKUP_DIR/requirements.txt" ]] && cp -a "$BACKUP_DIR/requirements.txt" "$APP_DIR/requirements.txt"
    if [[ -f "$BACKUP_DIR/radio-intelligence-api.service" ]]; then
      cp -a "$BACKUP_DIR/radio-intelligence-api.service" /etc/systemd/system/radio-intelligence-api.service
      systemctl daemon-reload
    fi
    chown -R radio:radio "$APP_DIR"
    systemctl start "$SERVICE" || true
  fi
  exit $status
}
trap rollback ERR

rm -rf "$APP_DIR/app"
cp -a "$SOURCE_DIR/app" "$APP_DIR/app"
cp "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SOURCE_DIR/VERSION" "$APP_DIR/VERSION"
cp "$SOURCE_DIR/deploy/radio-intelligence-api.service" /etc/systemd/system/radio-intelligence-api.service

"$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt" >/dev/null
"$APP_DIR/venv/bin/python" -m compileall -q "$APP_DIR/app"
chown -R radio:radio "$APP_DIR"
systemctl daemon-reload
systemctl start "$SERVICE"

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a
port="${RADIO_API_PORT:-8788}"

for attempt in $(seq 1 20); do
  if "$APP_DIR/venv/bin/python" - "$port" <<'PY' >/dev/null 2>&1
import json
import sys
import urllib.request
port = int(sys.argv[1])
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
    body = json.load(response)
    if response.status != 200 or body.get("status") not in {"ok", "degraded"}:
        raise SystemExit(1)
PY
  then
    break
  fi
  if [[ $attempt -eq 20 ]]; then
    echo "[radio-api-upgrade] Health check failed" >&2
    journalctl -u "$SERVICE" -n 80 --no-pager >&2 || true
    false
  fi
  sleep 1
done

trap - ERR

echo "[radio-api-upgrade] Upgrade complete"
echo "[radio-api-upgrade] Installed version: $(cat "$APP_DIR/VERSION")"
echo "[radio-api-upgrade] Existing SQLite database and configuration were preserved"
