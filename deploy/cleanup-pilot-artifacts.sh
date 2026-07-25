#!/usr/bin/env bash
set -euo pipefail

APPLY=false
PURGE_S3=false
REMOVE_LEGACY_NLP=false
KEEP_DAYS=2
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    --purge-s3) PURGE_S3=true ;;
    --remove-legacy-nlp) REMOVE_LEGACY_NLP=true ;;
    --keep-days=*) KEEP_DAYS="${arg#*=}" ;;
    -h|--help)
      cat <<'HELP'
Usage: cleanup-pilot-artifacts.sh [--apply] [--purge-s3] [--remove-legacy-nlp] [--keep-days=N]

Default is dry-run. It removes only deployment ZIPs/extracted source directories
from /tmp and /home/ec2-user/work. It never removes installed applications,
models, configuration, SQLite data, S3 analysis data, or active systemd units.

--purge-s3 additionally removes old *.zip objects under s3://$BUCKET_NAME/bootstrap/
that are older than KEEP_DAYS. The current v0.3.0 archive is always retained.

--remove-legacy-nlp removes the old manual Step 4A/4B application/model files
only when the active station worker is not Step 4C. This saves disk, not CPU. Run
it only after the v0.3 backend has passed live acceptance and a backup exists.
HELP
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo '[cleanup] Run with sudo' >&2
  exit 1
fi

remove_path() {
  local path="$1"
  if $APPLY; then
    rm -rf -- "$path"
    echo "[cleanup] removed $path"
  else
    echo "[cleanup] would remove $path"
  fi
}

# Temporary/extracted source packages only. Installed runtime paths are excluded.
while IFS= read -r -d '' path; do
  remove_path "$path"
done < <(
  find /tmp /home/ec2-user/work -maxdepth 1 \
    \( -type f \( \
      -name 'radio-*.zip' -o \
      -name 'firemud-radio-*.zip' \
    \) -o -type d \( \
      -name 'radio-*-amazonlinux2023*' -o \
      -name 'firemud-radio-*-amazonlinux2023*' \
    \) \) \
    ! -name 'firemud-radio-backend-conversation-amazonlinux2023-v0.3.0*' \
    -print0 2>/dev/null
)

# Old compilation source is safe to keep for rollback. Remove only when a working
# binary exists, and never remove the model/binary/config.
if [[ -x /opt/firemud/llm-runtime/bin/llama-server && -d /opt/firemud/llm-runtime/src/llama.cpp/build ]]; then
  build_dir=/opt/firemud/llm-runtime/src/llama.cpp/build
  if $APPLY; then
    rm -rf -- "$build_dir"
    echo "[cleanup] removed compiled build tree $build_dir (runtime binary preserved)"
  else
    echo "[cleanup] would remove compiled build tree $build_dir (runtime binary preserved)"
  fi
fi

if $REMOVE_LEGACY_NLP; then
  if [[ -f /opt/radio-pipeline/automation-step4c/VERSION ]] || \
     grep -q 'automation-step4c' /etc/systemd/system/radio-pipeline-worker@.service 2>/dev/null; then
    echo '[cleanup] refusing legacy NLP removal: active Step 4C worker still depends on Step 4A/4B' >&2
    exit 1
  fi
  legacy_paths=(
    /opt/radio-pipeline/intelligence-step4a
    /opt/radio-pipeline/sentiment-step4b
    /opt/radio-pipeline/sentiment-venv
    /opt/radio-pipeline/models/multilingual-minilm-nli-int8
    /etc/radio-pipeline/intelligence-step4a.env
    /etc/radio-pipeline/sentiment-step4b.env
    /usr/local/bin/radio-analyze-transcript
    /usr/local/bin/radio-mentions-show
    /usr/local/bin/radio-score-sentiment
    /usr/local/bin/radio-sentiment-show
  )
  for path in "${legacy_paths[@]}"; do
    [[ -e "$path" || -L "$path" ]] || continue
    remove_path "$path"
  done
fi

if $PURGE_S3; then
  : "${BUCKET_NAME:?Set BUCKET_NAME before --purge-s3}"
  region="${AWS_REGION:-eu-north-1}"
  cutoff="$(date -u -d "-${KEEP_DAYS} days" +%s)"
  aws s3api list-objects-v2 \
    --bucket "$BUCKET_NAME" \
    --prefix bootstrap/ \
    --region "$region" \
    --output json | jq -r '.Contents[]? | select(.Key | endswith(".zip")) | [.Key,.LastModified] | @tsv' |
  while IFS=$'\t' read -r key modified; do
    [[ "$key" == *'firemud-radio-backend-conversation-amazonlinux2023-v0.3.0.zip' ]] && continue
    epoch="$(date -u -d "$modified" +%s)"
    (( epoch >= cutoff )) && continue
    if $APPLY; then
      aws s3api delete-object --bucket "$BUCKET_NAME" --key "$key" --region "$region" >/dev/null
      echo "[cleanup] removed s3://${BUCKET_NAME}/${key}"
    else
      echo "[cleanup] would remove s3://${BUCKET_NAME}/${key}"
    fi
  done
fi

if ! $APPLY; then
  echo '[cleanup] dry-run only; rerun with --apply after reviewing the list'
fi
