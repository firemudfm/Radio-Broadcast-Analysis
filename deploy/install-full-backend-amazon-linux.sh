#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE=/etc/firemud/radio-intelligence.env

log() { printf '[backend-install] %s\n' "$*"; }
die() { printf '[backend-install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "Run with sudo"
[[ -f "$CONFIG_FILE" ]] || die "Existing FastAPI config is missing: $CONFIG_FILE"

append_if_missing() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" "$CONFIG_FILE"; then
    printf '%s="%s"\n' "$key" "$value" >> "$CONFIG_FILE"
  fi
}

log "0/5 Auditing the existing speech pipeline"
for required in \
  /opt/radio-pipeline/filter-venv/bin/python \
  /opt/radio-pipeline/transcribe-venv/bin/python \
  /etc/radio-pipeline/filter-step2a.env \
  /etc/radio-pipeline/transcribe-step3a.env; do
  [[ -e "$required" ]] || die "Required existing pipeline component is missing: $required"
done
if ! systemctl list-unit-files 'radio-pipeline-worker@.service' --no-legend 2>/dev/null | grep -q radio-pipeline-worker; then
  die "The automatic filter/transcription worker unit is missing"
fi

log "1/5 Installing the local small multilingual LLM"
"$SOURCE_DIR/deploy/install-llm-amazon-linux.sh"

log "2/5 Upgrading FastAPI in place"
"$SOURCE_DIR/deploy/upgrade-amazon-linux.sh"

append_if_missing RADIO_ANALYSIS_PREFIX "results/conversation-analysis/"
append_if_missing RADIO_TRANSCRIPTS_PREFIX "transcripts/"
append_if_missing RADIO_CONVERSATION_MAX_TRANSCRIPTS "200"
append_if_missing RADIO_CONVERSATION_SCAN_CHUNKS "6"
append_if_missing RADIO_CONVERSATION_SESSION_GAP_SECONDS "30"
append_if_missing RADIO_CONVERSATION_MAX_DURATION_SECONDS "1800"
append_if_missing RADIO_CONVERSATION_MAX_CHARACTERS "120000"
append_if_missing RADIO_LLM_ENABLED "true"
append_if_missing RADIO_LLM_BASE_URL "http://127.0.0.1:8790"
append_if_missing RADIO_LLM_MODEL "qwen3-0.6b-q8"
append_if_missing RADIO_LLM_TIMEOUT_SECONDS "90"
append_if_missing RADIO_LLM_MAX_INPUT_CHARACTERS "40000"
append_if_missing RADIO_LLM_MAX_OUTPUT_TOKENS "480"
append_if_missing RADIO_LLM_TEMPERATURE "0.1"
append_if_missing RADIO_ANALYSIS_WORKER_ENABLED "true"
append_if_missing RADIO_ANALYSIS_WORKER_POLL_SECONDS "20"
append_if_missing RADIO_ANALYSIS_WORKER_BATCH_SIZE "2"
append_if_missing RADIO_ANALYSIS_RETRY_LIMIT "3"
append_if_missing RADIO_ANALYSIS_SETTLE_SECONDS "360"
append_if_missing RADIO_SEMANTIC_DISCOVERY_ENABLED "true"
append_if_missing RADIO_SEMANTIC_SCAN_LOOKBACK_DAYS "7"
append_if_missing RADIO_SEMANTIC_GROUPS_PER_CYCLE "1"
append_if_missing RADIO_SEMANTIC_KEYWORDS_PER_GROUP "10"
append_if_missing RADIO_SEMANTIC_DEFAULT_THRESHOLD "0.74"
append_if_missing RADIO_SEMANTIC_SETTLE_SECONDS "120"
append_if_missing RADIO_SEMANTIC_RESULTS_PREFIX "results/semantic-matches/"
chown root:radio "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

log "3/5 Restarting the API with full-transcript settings"
systemctl restart radio-intelligence-api
for attempt in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8788/healthz >/dev/null 2>&1; then
    break
  fi
  if [[ $attempt -eq 40 ]]; then
    journalctl -u radio-intelligence-api -n 120 --no-pager >&2 || true
    die "FastAPI health check failed"
  fi
  sleep 1
done

log "4/5 Installing the shared campaign transcript + LLM analysis worker"
"$SOURCE_DIR/deploy/install-analysis-worker.sh"

log "5/5 Running acceptance checks"
"$SOURCE_DIR/deploy/audit-backend.sh"

echo
log "Complete"
log "Existing SQLite campaigns, station cursors, AI models, and S3 data were preserved"
log "No model process was created per campaign; all campaigns share one transcript worker and one local LLM"
