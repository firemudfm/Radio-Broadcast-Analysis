#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/firemud/radio-intelligence-api
CONFIG_DIR=/etc/firemud
STATE_DIR=/var/lib/firemud/radio-intelligence-api
CONFIG_FILE="$CONFIG_DIR/radio-intelligence.env"

if [[ ${EUID} -ne 0 ]]; then
  echo "[radio-api-install] Run with sudo" >&2
  exit 1
fi

if ! grep -q '^ID="\?amzn"\?' /etc/os-release || ! grep -q '^VERSION_ID="\?2023"\?' /etc/os-release; then
  echo "[radio-api-install] Amazon Linux 2023 is required" >&2
  exit 1
fi

if ! id radio >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /sbin/nologin radio
fi

echo "[radio-api-install] Installing Python runtime"
dnf install -y ca-certificates python3.11 python3.11-pip

echo "[radio-api-install] Installing application"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR"
cp -a "$SOURCE_DIR/app" "$APP_DIR/app"
cp "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SOURCE_DIR/VERSION" "$APP_DIR/VERSION"
python3.11 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip wheel
"$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$CONFIG_FILE" ]]; then
  cp "$SOURCE_DIR/deploy/radio-intelligence.env.example" "$CONFIG_FILE"
  secret="$(python3.11 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i "s|RADIO_AUDIO_TOKEN_SECRET=\"REPLACED_BY_INSTALLER\"|RADIO_AUDIO_TOKEN_SECRET=\"$secret\"|" "$CONFIG_FILE"
fi
if [[ ! -f "$CONFIG_DIR/radio-stations.json" ]]; then
  cp "$SOURCE_DIR/deploy/radio-stations.example.json" "$CONFIG_DIR/radio-stations.json"
fi
cp "$SOURCE_DIR/deploy/radio-intelligence-api.service" /etc/systemd/system/radio-intelligence-api.service

chown -R radio:radio "$APP_DIR" "$STATE_DIR"
chown root:radio "$CONFIG_DIR" "$CONFIG_FILE" "$CONFIG_DIR/radio-stations.json"
chmod 0640 "$CONFIG_FILE" "$CONFIG_DIR/radio-stations.json"
chmod 0750 "$CONFIG_DIR" "$STATE_DIR"

systemctl daemon-reload

echo "[radio-api-install] Installation complete"
echo
echo "Next: edit $CONFIG_FILE and set RADIO_S3_BUCKET, then run:"
echo "  sudo systemctl enable --now radio-intelligence-api"
