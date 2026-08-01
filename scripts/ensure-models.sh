#!/usr/bin/env bash
# Ensure the locked models are present and verified on the data volume.
#
#   scripts/ensure-models.sh --all              # first install
#   scripts/ensure-models.sh --verify-only      # normal update
#   scripts/ensure-models.sh --role asr [--role llm] [--dry-run]
#
# VERIFY FIRST, DOWNLOAD ONLY WHAT IS MISSING. A model that already verifies is
# never re-fetched: these are hundreds of megabytes over a metered link, and
# re-downloading on every deployment makes a deploy depend on a third party
# being up.
#
# Models live on the data volume, outside Git and outside every image layer.
# They are NEVER downloaded at container start-up, API start-up or worker
# start-up -- an implicit download turns a routine restart into an outage when
# the provider is unreachable, and it happens with no operator watching.
#
# models.lock.json is the only source of truth. A model that is not in it is
# never fetched, and a revision is never floating.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/deploy-common.sh
source "${SCRIPT_DIR}/lib/deploy-common.sh"

REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODEL_ROOT="${RADIO_HOST_MODELS:-/var/lib/radio/models}"
LOCK="${RADIO_MODELS_LOCK:-${REPO_ROOT}/models.lock.json}"
#: A model download plus its temporary copy needs room for both at once.
FREE_MIB="${RADIO_MIN_MODEL_FREE_MIB:-4096}"

ROLES=()
VERIFY_ONLY=0
DRY_RUN=0

usage() {
    cat <<'USAGE'
Ensure locked models are present and verified. Downloads only what is missing.

Usage:
  scripts/ensure-models.sh --all           [--dry-run]
  scripts/ensure-models.sh --verify-only   [--role asr|llm|vad]...
  scripts/ensure-models.sh --role asr --role llm

Options:
  --all           Every role defined in models.lock.json.
  --role ROLE     asr | llm | vad. Repeatable.
  --verify-only   Verify and report; never download.
  --root PATH     Model root (default /var/lib/radio/models).
  --dry-run       Report what would be downloaded; download nothing.
  -h, --help      Show this help.

Never downloads at container, API or worker start-up. Never uses a model or a
revision that is not pinned in models.lock.json.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --all)         ROLES=(asr llm vad); shift ;;
        --role)        ROLES+=("${2:-}"); shift 2 ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        --root)        MODEL_ROOT="${2:-}"; shift 2 ;;
        --lock)        LOCK="${2:-}"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit "${EXIT_OK}" ;;
        *)             usage >&2; die "${EXIT_USAGE}" "unknown argument: $1" ;;
    esac
done

require_commands python3
[ -f "${LOCK}" ] || die "${EXIT_PRECONDITION}" "models lock not found: ${LOCK}"

if [ "${#ROLES[@]}" -eq 0 ]; then
    usage >&2
    die "${EXIT_USAGE}" "one of --all or --role is required"
fi
for role in "${ROLES[@]}"; do
    case "${role}" in
        asr|llm|vad) ;;
        *) die "${EXIT_USAGE}" "unknown role '${role}' (expected asr, llm or vad)" ;;
    esac
done

# Roles actually defined in the lock. `vad` is optional: models.lock.json
# documents it as optional and the classifier degrades to energy-only signals
# without it, so a lock without a VAD entry must not fail a deployment.
locked_roles() {
    python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    models = json.load(handle)["models"]
print(" ".join(sorted({m.get("role", "") for m in models.values()} - {""})))
' "${LOCK}"
}
LOCKED="$(locked_roles)"
log "roles pinned in the lock: ${LOCKED}"

# --require-present, always. A deployment asked for these roles explicitly, so
# "optional" must not mean "reported VERIFIED while absent" -- that is how a host
# silently ends up running without a model the lock pins. The general verifier
# keeps its permissive mode for callers that genuinely accept energy-only
# degradation.
verify_role() {
    python3 "${SCRIPT_DIR}/verify-models.py" \
        --root "${MODEL_ROOT}" --lock "${LOCK}" --role "$1" --require-present >/dev/null 2>&1
}

# role_files_present <role> -- any file for this role already on disk.
role_files_present() {
    python3 -c '
import json, os, sys
lock, root, role = sys.argv[1], sys.argv[2], sys.argv[3]
with open(lock, encoding="utf-8") as handle:
    models = json.load(handle)["models"]
for model in models.values():
    if model.get("role") != role:
        continue
    directory = os.path.join(root, model["target_directory"])
    for spec in model.get("files", []):
        if os.path.exists(os.path.join(directory, spec["name"])):
            print("present")
            raise SystemExit(0)
print("absent")
' "${LOCK}" "${MODEL_ROOT}" "$1"
}

VERIFIED=()
DOWNLOADED=()
SKIPPED=()

stage "Ensuring models under ${MODEL_ROOT}"
mkdir -p "${MODEL_ROOT}" 2>/dev/null || true

for role in "${ROLES[@]}"; do
    case " ${LOCKED} " in
        *" ${role} "*) ;;
        *)
            if [ "${role}" = "vad" ]; then
                log "vad: not pinned in the lock; skipping (it is optional by design)"
                SKIPPED+=("vad")
                continue
            fi
            die "${EXIT_PRECONDITION}" "role '${role}' is not pinned in ${LOCK}"
            ;;
    esac

    # 1. Verify first. A model that already verifies is never touched again.
    if verify_role "${role}"; then
        log "${role}: VERIFIED (already present; not downloading)"
        VERIFIED+=("${role}")
        continue
    fi

    presence="$(role_files_present "${role}")"

    if [ "${presence}" = "present" ]; then
        # 4. Files exist but do not verify. Never used, never overwritten,
        #    never deleted: a truncated or tampered model is evidence, and
        #    silently replacing it hides both the cause and the fact that the
        #    system was running on something unverified.
        fail "${role}: files exist under ${MODEL_ROOT} but FAILED verification"
        python3 "${SCRIPT_DIR}/verify-models.py" \
            --root "${MODEL_ROOT}" --lock "${LOCK}" --role "${role}" --require-present >&2 || true
        remediation "inspect the paths above, then remove them explicitly if you are satisfied they are corrupt"
        die "${EXIT_PRECONDITION}" \
            "refusing to overwrite or delete an existing model that does not verify"
    fi

    # Absent.
    if [ "${VERIFY_ONLY}" -eq 1 ]; then
        # A normal update still fetches a locked model that has gone missing --
        # the alternative is starting a worker that has nothing to transcribe
        # with. Verify-only is about not re-downloading what is already good.
        log "${role}: absent"
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "${role}: dry run; would download from the pinned revision"
        continue
    fi

    require_free_space "${MODEL_ROOT}" "${FREE_MIB}"
    log "${role}: absent; downloading the pinned revision"
    # download-models.py fetches to a temporary path, checks size and digest
    # against the lock, and installs atomically. Any output it produces is
    # paths and digests -- never a token or a presigned URL.
    python3 "${SCRIPT_DIR}/download-models.py" \
        --root "${MODEL_ROOT}" --lock "${LOCK}" --role "${role}" \
        || die "${EXIT_PRECONDITION}" "download failed for role ${role}"

    # 3. Verify again after installation. A download that reports success but
    #    produces a file that does not verify must not reach a worker.
    verify_role "${role}" \
        || die "${EXIT_PRECONDITION}" "role ${role} still does not verify after download"
    log "${role}: downloaded and VERIFIED"
    DOWNLOADED+=("${role}")
done

stage "Model summary"
log "verified (untouched): ${#VERIFIED[@]}${VERIFIED:+ (${VERIFIED[*]})}"
log "downloaded now:       ${#DOWNLOADED[@]}${DOWNLOADED:+ (${DOWNLOADED[*]})}"
log "skipped (optional):   ${#SKIPPED[@]}${SKIPPED:+ (${SKIPPED[*]})}"
exit "${EXIT_OK}"
