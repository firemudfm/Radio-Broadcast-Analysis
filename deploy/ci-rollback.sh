#!/usr/bin/env bash
# Restores the newest pre-deploy backup of the app code and restarts the
# backend services. Used by the CD rollback job and the manual Rollback
# workflow; safe to run by hand on the instance too.
set -euo pipefail

APP=/opt/firemud/radio-intelligence-api
BACKUP_ROOT=/var/backups
SERVICES=(radio-intelligence-api radio-station-reconciler radio-analysis-worker)

LATEST=$(sudo sh -c "ls -dt $BACKUP_ROOT/radio-app-backup-* 2>/dev/null | head -1" || true)
[ -n "$LATEST" ] || { echo "No backup found under $BACKUP_ROOT; nothing to roll back to"; exit 1; }

echo "Rolling back to $LATEST"
sudo rsync -a --delete "$LATEST/" "$APP/app/"
sudo systemctl restart "${SERVICES[@]}"
sleep 5

for service in "${SERVICES[@]}"; do
  systemctl is-active "$service"
done
curl -sf --max-time 10 http://127.0.0.1:8788/healthz
echo
echo "Rollback complete"
