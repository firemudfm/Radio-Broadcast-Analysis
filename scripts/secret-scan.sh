#!/usr/bin/env bash
# Fail if anything that looks like a credential is about to ship.
#
# In a git repository this scans TRACKED files only. Untracked local files are
# the developer's business; what matters is that nothing reaches a push, an
# image layer, or a clone. Run against an extracted release directory, which has
# no .git, it scans the tree instead -- see SCAN_MODE below.
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

# In a repository the scan is authoritative: it inspects exactly what git
# tracks. But deploy-compose.sh also runs this inside a release directory built
# by `git archive`, which has no .git -- and there every `git grep` matched
# nothing, every `git ls-files` returned nothing, and the script printed PASS
# without having read a single file. A gate that silently inspects nothing is
# worse than no gate, because the deploy log then says "release secret scan
# passed". Outside a repository the same patterns are applied to the tree.
#
# The `.git` test is not redundant with `--is-inside-work-tree`. That check
# walks UP the directory tree, so a release extracted anywhere beneath an
# unrelated repository would report "inside a work tree" and every `git grep`
# would then scan that ANCESTOR repository instead of the release -- reporting
# PASS about a directory it never looked at. Requiring .git here means git mode
# is used only for the tree actually rooted at this directory.
if [ -e .git ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SCAN_MODE="git"
else
    SCAN_MODE="filesystem"
fi

# scan <grep-flags-and-pattern...>  -- prints matches, exit 0 when found.
scan() {
    if [ "${SCAN_MODE}" = "git" ]; then
        git grep "$@" -- . ':(exclude)scripts/secret-scan.sh'
    else
        grep --binary-files=without-match -r "$@" . \
            --exclude-dir=.git --exclude="secret-scan.sh" 2>/dev/null
    fi
}

# list_files -- every file this scan considers, repo-relative.
list_files() {
    if [ "${SCAN_MODE}" = "git" ]; then
        git ls-files
    else
        find . -type f -not -path './.git/*' | sed 's|^\./||'
    fi
}

# is_present <path> -- tracked in the repo, or present in the release tree.
is_present() {
    if [ "${SCAN_MODE}" = "git" ]; then
        git ls-files --error-unmatch "$1" >/dev/null 2>&1
    else
        [ -f "$1" ]
    fi
}

echo "==> Scanning files for credential patterns (${SCAN_MODE} mode)"

# AWS access key ids have a fixed, unmistakable shape.
if scan -nE '(AKIA|ASIA)[0-9A-Z]{16}' >/dev/null 2>&1; then
    scan -nE '(AKIA|ASIA)[0-9A-Z]{16}' >&2
    report "an AWS access key id is present"
fi

# A 40-character base64-ish secret assigned to an aws secret variable.
if scan -niE 'aws_secret_access_key[[:space:]]*[:=][[:space:]]*[A-Za-z0-9/+=]{40}' \
        >/dev/null 2>&1; then
    report "an AWS secret access key is present"
fi

# Private keys of any flavour.
if scan -nE 'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY' >/dev/null 2>&1; then
    report "a private key is present"
fi

echo "==> Checking that real environment files are not tracked"
# deploy/dev/* is intentionally tracked: those values are placeholders that
# grant nothing. Anything at the repository root is not.
for path in .env application.env infrastructure.env \
            deploy/application.env deploy/infrastructure.env; do
    if is_present "${path}"; then
        report "${path} is present and must not be"
    fi
done

echo "==> Checking the audio token secret"
# The one secret with a validated minimum length; a real one is >= 32 chars.
while IFS= read -r match; do
    case "${match}" in
        *replace-me*|*development-only*|*RADIO_AUDIO_TOKEN_SECRET=$*|*x*x*x*) continue ;;
    esac
    file="${match%%:*}"
    # grep -r reports ./tests/x, git grep reports tests/x. The exclusions below
    # are written repo-relative, so normalise before matching them.
    file="${file#./}"
    case "${file}" in
        .env.example|deploy/dev/*|docs/*|tests/*|scripts/secret-scan.sh) continue ;;
    esac
    report "a non-placeholder RADIO_AUDIO_TOKEN_SECRET appears in ${file}"
done < <(scan -nE 'RADIO_AUDIO_TOKEN_SECRET[[:space:]]*[:=][[:space:]]*[^$"'"'"' ]{16,}' 2>/dev/null || true)

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

echo "==> Checking that model binaries are not present"
if list_files | grep -qE '\.(gguf|onnx|bin|pt|pth)$'; then
    list_files | grep -E '\.(gguf|onnx|bin|pt|pth)$' >&2
    report "a model binary is present; models are fetched with scripts/download-models.py"
fi

echo
if [ "${FAILURES}" -gt 0 ]; then
    echo "secret-scan: FAILED (${FAILURES} finding(s))" >&2
    exit 1
fi
echo "secret-scan: PASS"
