#!/usr/bin/env bash
# Runs ON the EC2 instance, invoked by .github/workflows/deploy.yml after the
# checkout was rsynced to /tmp/radio-ci-deploy. Installs the new app code into
# the live service, restarts the backend services, health-checks the API, and
# rolls back to the previous code if the health check fails.
#
# Restarts: radio-intelligence-api, radio-station-reconciler,
# radio-analysis-worker. Per-station capture/uploader/worker units are NOT
# restarted: they run the separate ingestion package and restarting them would
# interrupt live recordings for no reason.
set -euo pipefail

SRC=/tmp/radio-ci-deploy
APP=/opt/firemud/radio-intelligence-api
SERVICES=(radio-intelligence-api radio-station-reconciler radio-analysis-worker)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/tmp/radio-app-backup-$STAMP

[ -d "$SRC/app" ] || { echo "No synced code at $SRC/app"; exit 1; }
sudo test -d "$APP/app" || { echo "Live app not found at $APP/app"; exit 1; }

# Strip Windows line endings that may survive a checkout, then compile-check
# BEFORE touching the live service.
find "$SRC/app" -name '*.py' -exec sed -i 's/\r$//' {} +
sed -i 's/\r$//' "$SRC/requirements.txt"
sudo "$APP/venv/bin/python" -m compileall -q "$SRC/app" || {
  echo "New code does not compile; aborting before touching the live app"
  exit 1
}

# Dependencies (no-op unless requirements.txt changed).
sudo "$APP/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"

# Swap the code in, keeping a rollback copy of what was live.
sudo cp -a "$APP/app" "$BACKUP"
sudo rsync -a --delete "$SRC/app/" "$APP/app/"

sudo systemctl restart "${SERVICES[@]}"
sleep 5

restart_ok=true
for service in "${SERVICES[@]}"; do
  systemctl is-active --quiet "$service" || { echo "$service is not active"; restart_ok=false; }
done
health_ok=true
curl -sf --max-time 10 http://127.0.0.1:8788/healthz >/dev/null || health_ok=false

if [ "$restart_ok" = true ] && [ "$health_ok" = true ]; then
  echo "Deploy OK: services active, /healthz responding"
  curl -s http://127.0.0.1:8788/healthz
  echo
  # Keep only the five newest rollback copies.
  ls -dt /tmp/radio-app-backup-* 2>/dev/null | tail -n +6 | xargs -r sudo rm -rf
  exit 0
fi

echo "Deploy FAILED health checks; rolling back to $BACKUP"
sudo rsync -a --delete "$BACKUP/" "$APP/app/"
sudo systemctl restart "${SERVICES[@]}"
sleep 5
systemctl is-active "${SERVICES[@]}" || true
curl -s http://127.0.0.1:8788/healthz || true
echo
echo "Rollback applied; the failing commit was NOT deployed"
exit 1
