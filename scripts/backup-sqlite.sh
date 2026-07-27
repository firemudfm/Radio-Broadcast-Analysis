#!/usr/bin/env bash
# Take a consistent SQLite backup while the stack keeps running.
#
# Uses `sqlite3 .backup`, NOT `cp`. In WAL mode the database is two or three
# files whose contents change between reads, so copying them individually can
# capture a torn state that restores as a corrupt database -- and you find out
# at the moment you most need the backup. `.backup` takes a proper snapshot
# through the SQLite API and works against a live, actively written database.
set -euo pipefail

DATABASE="${RADIO_DATABASE_PATH:-/var/lib/radio/database/radio.db}"
BACKUP_DIR="${RADIO_HOST_BACKUPS:-/var/lib/radio/backups}"
RETENTION_DAYS="${RADIO_BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/radio-${STAMP}.db"

if [ ! -f "${DATABASE}" ]; then
    echo "backup-sqlite: no database at ${DATABASE}" >&2
    exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "backup-sqlite: sqlite3 is not installed" >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"
# 0700: backups contain full transcripts, which are the product's output.
chmod 0700 "${BACKUP_DIR}"

echo "==> Backing up ${DATABASE}"
sqlite3 "${DATABASE}" ".backup '${TARGET}'"

echo "==> Verifying the backup"
# integrity_check on the COPY, not the original: a backup nobody verified is a
# hope, not a backup.
RESULT="$(sqlite3 "${TARGET}" 'PRAGMA integrity_check;')"
if [ "${RESULT}" != "ok" ]; then
    echo "backup-sqlite: integrity check failed: ${RESULT}" >&2
    rm -f "${TARGET}"
    exit 1
fi

SIZE="$(wc -c < "${TARGET}")"
echo "    ${TARGET} (${SIZE} bytes) integrity ok"

if command -v gzip >/dev/null 2>&1; then
    gzip -9 "${TARGET}"
    TARGET="${TARGET}.gz"
    echo "    compressed to ${TARGET}"
fi
chmod 0600 "${TARGET}"

echo "==> Pruning backups older than ${RETENTION_DAYS} days"
# -mtime only; nothing here consults database state, so this cannot delete
# anything but an old backup file.
find "${BACKUP_DIR}" -maxdepth 1 -name 'radio-*.db*' -type f \
    -mtime "+${RETENTION_DAYS}" -print -delete || true

REMAINING="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'radio-*.db*' -type f | wc -l)"
echo "    ${REMAINING} backup(s) retained"

if [ -n "${RADIO_S3_BUCKET:-}" ] && command -v aws >/dev/null 2>&1; then
    echo "==> Uploading to s3://${RADIO_S3_BUCKET}/backups/sqlite/"
    aws s3 cp "${TARGET}" "s3://${RADIO_S3_BUCKET}/backups/sqlite/" \
        --sse AES256 --only-show-errors
    echo "    uploaded"
else
    echo "==> Skipping S3 upload (RADIO_S3_BUCKET unset or aws CLI absent)"
fi

echo
echo "backup-sqlite: PASS"
