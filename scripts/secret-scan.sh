#!/usr/bin/env bash
# Fail if anything that looks like a credential is tracked by git.
#
# Scans TRACKED files only. Untracked local files are the developer's business;
# what matters is that nothing reaches a push, an image layer, or a clone.
# A credential in git history is compromised the moment it lands, and rewriting
# history afterwards does not un-leak it -- which is why this is a gate rather
# than a lint.
set -uo pipefail

cd "$(dirname "$0")/.."

FAILURES=0

report() {
    echo "  - $1" >&2
    FAILURES=$((FAILURES + 1))
}

echo "==> Scanning tracked files for credential patterns"

# AWS access key ids have a fixed, unmistakable shape.
if git grep -nE '(AKIA|ASIA)[0-9A-Z]{16}' -- . ':(exclude)scripts/secret-scan.sh' >/dev/null 2>&1; then
    git grep -nE '(AKIA|ASIA)[0-9A-Z]{16}' -- . ':(exclude)scripts/secret-scan.sh' >&2
    report "an AWS access key id is tracked"
fi

# A 40-character base64-ish secret assigned to an aws secret variable.
if git grep -niE 'aws_secret_access_key[[:space:]]*[:=][[:space:]]*[A-Za-z0-9/+=]{40}' \
        -- . ':(exclude)scripts/secret-scan.sh' >/dev/null 2>&1; then
    report "an AWS secret access key is tracked"
fi

# Private keys of any flavour.
if git grep -nE 'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY' \
        -- . ':(exclude)scripts/secret-scan.sh' >/dev/null 2>&1; then
    report "a private key is tracked"
fi

echo "==> Checking that real environment files are not tracked"
# deploy/dev/* is intentionally tracked: those values are placeholders that
# grant nothing. Anything at the repository root is not.
for path in .env application.env infrastructure.env \
            deploy/application.env deploy/infrastructure.env; do
    if git ls-files --error-unmatch "${path}" >/dev/null 2>&1; then
        report "${path} is tracked and must not be"
    fi
done

echo "==> Checking the audio token secret"
# The one secret with a validated minimum length; a real one is >= 32 chars.
while IFS= read -r match; do
    case "${match}" in
        *replace-me*|*development-only*|*RADIO_AUDIO_TOKEN_SECRET=$*|*x*x*x*) continue ;;
    esac
    file="${match%%:*}"
    case "${file}" in
        .env.example|deploy/dev/*|docs/*|tests/*|scripts/secret-scan.sh) continue ;;
    esac
    report "a non-placeholder RADIO_AUDIO_TOKEN_SECRET appears in ${file}"
done < <(git grep -nE 'RADIO_AUDIO_TOKEN_SECRET[[:space:]]*[:=][[:space:]]*[^$"'"'"' ]{16,}' -- . 2>/dev/null || true)

echo "==> Checking public example configuration for production identifiers"
# NOT a credential check -- an AWS account id is not a secret. This is a
# separate category: a real account id, live endpoint or generated bucket name
# in a PUBLIC template is free reconnaissance, and a realistic-looking value
# invites somebody to copy it into a deployment where it half-works.
#
# Scoped to .env.example only. Docs, ADRs, CloudFormation outputs and test
# fixtures legitimately reference real infrastructure and are not scanned.
#
# Matched by SHAPE, never by literal: storing the real account id here would
# reintroduce the value this check exists to keep out.
if [ -f .env.example ]; then
    # Strip comments first; explanatory prose may legitimately mention formats.
    example_values="$(grep -vE '^[[:space:]]*#' .env.example || true)"

    if printf '%s' "${example_values}" | grep -qE '(^|[^0-9])[0-9]{12}([^0-9]|$)'; then
        printf '%s' "${example_values}" | grep -nE '(^|[^0-9])[0-9]{12}([^0-9]|$)' >&2
        report "production identifier found in public example configuration: AWS account id"
    fi
    if printf '%s' "${example_values}" | grep -q 'amazonaws\.com'; then
        printf '%s' "${example_values}" | grep -n 'amazonaws\.com' >&2
        report "production identifier found in public example configuration: live AWS endpoint"
    fi
fi

echo "==> Checking that model binaries are not tracked"
if git ls-files | grep -qE '\.(gguf|onnx|bin|pt|pth)$'; then
    git ls-files | grep -E '\.(gguf|onnx|bin|pt|pth)$' >&2
    report "a model binary is tracked; models are fetched with scripts/download-models.py"
fi

echo
if [ "${FAILURES}" -gt 0 ]; then
    echo "secret-scan: FAILED (${FAILURES} finding(s))" >&2
    exit 1
fi
echo "secret-scan: PASS"
