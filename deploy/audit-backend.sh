#!/usr/bin/env bash
set -Eeuo pipefail

services=(radio-intelligence-api radio-llm radio-analysis-worker)
shopt -s nullglob
for config in /etc/radio-pipeline/stations/*.env; do
  station_id="$(basename "$config" .env)"
  services+=("radio-capture@${station_id}" "radio-uploader@${station_id}")
  if [[ -f "/etc/radio-pipeline/automation/${station_id}.env" ]]; then
    services+=("radio-pipeline-worker@${station_id}")
  fi
done
shopt -u nullglob

printf '%-46s %-12s %-12s\n' SERVICE ACTIVE ENABLED
printf '%-46s %-12s %-12s\n' '----------------------------------------------' '------------' '------------'
failed=0
for service in "${services[@]}"; do
  active="$(systemctl is-active "$service" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$service" 2>/dev/null || true)"
  printf '%-46s %-12s %-12s\n' "$service" "$active" "$enabled"
  [[ "$active" == active && "$enabled" == enabled ]] || failed=1
done

echo
printf 'Installed versions:\n'
for version_file in \
  /opt/firemud/radio-intelligence-api/VERSION \
  /opt/radio-pipeline/automation-step4c/VERSION \
  /opt/radio-pipeline/transcribe-step3a/VERSION; do
  if [[ -r "$version_file" ]]; then
    printf '  %-58s %s\n' "$version_file" "$(cat "$version_file")"
  fi
done

echo
if command -v curl >/dev/null 2>&1; then
  echo 'FastAPI health:'
  health_json="$(curl -fsS http://127.0.0.1:8788/healthz)" || health_json=''
  echo "$health_json" | jq . || failed=1
  echo "$health_json" | jq -e '
    .status == "ok" and
    .database == "ok" and
    .s3 == "ok" and
    .llm == "ok" and
    .analysis_worker_enabled == true
  ' >/dev/null || failed=1
  echo
  echo 'Backend runtime:'
  runtime_json="$(curl -fsS http://127.0.0.1:8788/api/v1/brand-signal/runtime)" || runtime_json=''
  echo "$runtime_json" | jq . || failed=1
  echo "$runtime_json" | jq -e '
    .llm_health == "ok" and
    .analysis_worker_enabled == true and
    .semantic_discovery_enabled == true
  ' >/dev/null || failed=1
fi

echo
printf 'Disk usage:\n'
du -sh /opt/radio-pipeline /opt/firemud /var/lib/radio-pipeline /var/lib/firemud 2>/dev/null || true

echo
printf 'Memory/CPU snapshot:\n'
free -h || true
uptime || true

if [[ $failed -ne 0 ]]; then
  echo '[backend-audit] One or more required services/checks failed' >&2
  exit 1
fi
echo '[backend-audit] PASS'
