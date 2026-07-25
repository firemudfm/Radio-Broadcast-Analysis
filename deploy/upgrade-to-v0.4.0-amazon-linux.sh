#!/usr/bin/env bash
# Upgrade the FireMud radio intelligence backend to v0.4.0 on Amazon Linux 2023.
# Run as root from the extracted package directory:
#   sudo ./deploy/upgrade-to-v0.4.0-amazon-linux.sh
#
# Preserves: /etc/firemud/radio-intelligence.env (audio-token secret included),
# SQLite data (backed up via the SQLite backup API), Whisper/Qwen/filter models,
# S3 data, and every running station pipeline (hertz879 is never restarted).
# Rolls back /opt and the DB automatically when the post-upgrade health check
# fails.
set -euo pipefail

APP_DIR="/opt/firemud/radio-intelligence-api"
ENV_FILE="/etc/firemud/radio-intelligence.env"
DB_PATH_DEFAULT="/var/lib/firemud/radio-intelligence-api/radio.db"
BACKUP_ROOT="/var/backups/firemud"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/pre-v0.4.0-${STAMP}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '[upgrade] %s\n' "$*"; }
die() { printf '[upgrade] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE is missing; this host is not an installed backend"
[[ -d "$APP_DIR" ]] || die "$APP_DIR is missing"

# shellcheck disable=SC1090
source "$ENV_FILE"
DB_PATH="${RADIO_DATABASE_PATH:-$DB_PATH_DEFAULT}"

log "1/8 Backing up current install to ${BACKUP_DIR}"
mkdir -p "$BACKUP_DIR"
cp -a "$APP_DIR" "$BACKUP_DIR/radio-intelligence-api"
cp -a "$ENV_FILE" "$BACKUP_DIR/radio-intelligence.env"
if [[ -f "$DB_PATH" ]]; then
  # SQLite online backup API: consistent even while the API is running.
  python3 - "$DB_PATH" "$BACKUP_DIR/radio.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
print("sqlite backup complete")
PY
fi

log "2/8 Installing v0.4.0 source into ${APP_DIR}"
rsync -a --delete \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  "$SRC_DIR/app" "$SRC_DIR/tools" "$SRC_DIR/requirements.txt" "$SRC_DIR/requirements-dev.txt" \
  "$SRC_DIR/VERSION" "$APP_DIR/"

log "3/8 Updating Python dependencies (no model downloads)"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

log "4/8 Merging new environment defaults (existing values preserved)"
while IFS= read -r line; do
  [[ "$line" =~ ^[A-Z_]+= ]] || continue
  key="${line%%=*}"
  grep -q "^${key}=" "$ENV_FILE" || { echo "$line" >> "$ENV_FILE"; log "  added ${key}"; }
done < "$SRC_DIR/deploy/radio-intelligence.env.example"

log "5/8 Applying the v0.4 SQLite migration (idempotent)"
"$APP_DIR/venv/bin/python" - <<PY
import sys
sys.path.insert(0, "$APP_DIR")
from pathlib import Path
from app.db import Database
from app.db_catalog import CatalogStore
database = Database(Path("$DB_PATH"))
database.connect()
CatalogStore(database).migrate()
database.close()
print("migration complete")
PY

log "6/8 Installing the station reconciler service"
install -m 0644 "$SRC_DIR/deploy/radio-station-reconciler.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable radio-station-reconciler.service

log "7/8 Restarting only the API and reconciler (station pipelines untouched)"
systemctl restart radio-intelligence-api
systemctl restart radio-station-reconciler

log "8/8 Health check"
sleep 3
health="$(curl -sf --max-time 15 "http://127.0.0.1:${RADIO_API_PORT:-8788}/healthz" || true)"
if [[ "$health" == *'"status":"ok"'* && "$health" == *'"version":"0.4.0"'* ]]; then
  log "SUCCESS: $health"
  catalog="$(curl -sf --max-time 20 "http://127.0.0.1:${RADIO_API_PORT:-8788}/api/v1/monitoring/capacity" || true)"
  log "capacity: ${catalog:-unavailable}"
  exit 0
fi

log "Health check FAILED (${health:-no response}); rolling back"
systemctl stop radio-station-reconciler || true
rsync -a --delete --exclude 'venv' "$BACKUP_DIR/radio-intelligence-api/" "$APP_DIR/"
cp -a "$BACKUP_DIR/radio-intelligence.env" "$ENV_FILE"
if [[ -f "$BACKUP_DIR/radio.db" ]]; then
  systemctl stop radio-intelligence-api
  cp -a "$BACKUP_DIR/radio.db" "$DB_PATH"
fi
systemctl restart radio-intelligence-api
systemctl disable radio-station-reconciler.service || true
die "Rolled back to the pre-upgrade backup in ${BACKUP_DIR}"
