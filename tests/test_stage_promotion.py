"""Same-commit stage promotion: api -> core -> full without a new commit.

The blocker this closes: release identity was the commit alone, so
`releases/<sha>` could exist exactly once. Having deployed commit X at stage
`api`, deploying the SAME reviewed commit X at stage `core` was refused --
correctly, by the fail-closed check that stops an unverified directory being
reused. There was no way out of it except to produce a different Git commit
purely to widen deployment scope, which breaks the one guarantee this whole
deployment model exists to provide: what runs is exactly what was reviewed.

Release identity is now **commit + stage**:

    /var/lib/radio/releases/<sha>/api
    /var/lib/radio/releases/<sha>/core
    /var/lib/radio/releases/<sha>/full

Each is an independent immutable release, materialised by its own
`git archive` of the same commit -- never copied from a sibling, never mutated,
never re-stamped.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
LIB = SCRIPTS / "lib" / "deploy-common.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available on this host")

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_PRECONDITION = 65

STAGES = ("api", "core", "full")


def run_snippet(snippet: str, *, path_prefix: Path | None = None, **env):
    script = f'set -euo pipefail\nsource "{LIB.as_posix()}"\n{textwrap.dedent(snippet)}'
    environment = {**os.environ, **{k: str(v) for k, v in env.items()}}
    if path_prefix is not None:
        environment["PATH"] = f"{path_prefix.as_posix()}{os.pathsep}{environment['PATH']}"
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [BASH, "-c", script],
        capture_output=True, text=True, timeout=180, check=False, env=environment,
    )


@pytest.fixture(scope="module")
def head_sha() -> str:
    return subprocess.run(  # noqa: S603 - fixed argument array
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, cwd=str(REPO_ROOT),
    ).stdout.strip()


def create(root: Path, commit: str, stage: str):
    return run_snippet(
        f'create_release "{REPO_ROOT.as_posix()}" "{commit}" "{stage}" "{root.as_posix()}"'
    )


def stamp(release: Path, commit: str, stage: str):
    return run_snippet(
        f'write_release_manifest "{release.as_posix()}" "{commit}" "{stage}"'
    )


# =============================================================================
# A. The blocker, and that it is gone
# =============================================================================


def test_the_same_commit_can_be_promoted_through_every_stage(
    tmp_path: Path, head_sha: str
) -> None:
    """THE blocker. Widening scope must not require a different Git commit.

    Needing a new commit to go from api to core would mean the thing running in
    production is not the thing that was reviewed -- which is the entire point
    of deploying an exact SHA.
    """
    root = tmp_path / "releases"
    root.mkdir()

    for stage in STAGES:
        result = create(root, head_sha, stage)
        assert result.returncode == EXIT_OK, f"{stage}: {result.stderr}"
        assert result.stdout == (root / head_sha / stage).as_posix()
        assert stamp(root / head_sha / stage, head_sha, stage).returncode == EXIT_OK

    for stage in STAGES:
        release = root / head_sha / stage
        assert (release / "compose.yaml").is_file()
        assert (release / "VERSION").is_file()
        assert not (release / ".git").exists()
        manifest = json.loads((release / ".release-manifest.json").read_text())
        assert manifest["commit"] == head_sha, "every stage pins the same commit"
        assert manifest["stage"] == stage, "each records its own stage"
        assert manifest["source"] == "git archive"


def test_each_stage_is_archived_independently(tmp_path: Path, head_sha: str) -> None:
    """A stage must never be a copy of a sibling.

    Copying would make the second release's contents depend on whatever
    happened to the first one after it was created.
    """
    root = tmp_path / "releases"
    root.mkdir()
    create(root, head_sha, "api")
    # Tamper with the first stage. A copy-based implementation would carry this
    # into the second; a fresh `git archive` cannot.
    (root / head_sha / "api" / "compose.yaml").write_text("tampered\n", encoding="utf-8")

    assert create(root, head_sha, "core").returncode == EXIT_OK
    core_compose = (root / head_sha / "core" / "compose.yaml").read_text(encoding="utf-8")
    assert "tampered" not in core_compose
    assert core_compose == (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")


def test_a_sibling_stage_never_blocks_another(tmp_path: Path, head_sha: str) -> None:
    root = tmp_path / "releases"
    root.mkdir()
    assert create(root, head_sha, "core").returncode == EXIT_OK
    for other in ("api", "full"):
        assert create(root, head_sha, other).returncode == EXIT_OK, other
    assert sorted(p.name for p in (root / head_sha).iterdir()) == ["api", "core", "full"]


# =============================================================================
# B. Re-deploying the exact same commit AND stage is still refused
# =============================================================================


def test_the_exact_same_commit_and_stage_is_still_refused(
    tmp_path: Path, head_sha: str
) -> None:
    """Widening the identity must not weaken the integrity check.

    A directory that already exists still cannot be shown to match the reviewed
    commit, so it is still never reused.
    """
    root = tmp_path / "releases"
    root.mkdir()
    assert create(root, head_sha, "api").returncode == EXIT_OK
    again = create(root, head_sha, "api")
    assert again.returncode == EXIT_PRECONDITION
    assert "unverified release directory" in again.stderr


def test_a_refused_redeploy_overwrites_nothing(tmp_path: Path, head_sha: str) -> None:
    root = tmp_path / "releases"
    root.mkdir()
    create(root, head_sha, "api")
    release = root / head_sha / "api"
    stamp(release, head_sha, "api")

    marker = release / "OPERATOR_WAS_HERE"
    marker.write_text("evidence\n", encoding="utf-8")
    before = (release / ".release-manifest.json").read_text(encoding="utf-8")

    assert create(root, head_sha, "api").returncode != EXIT_OK
    assert marker.read_text(encoding="utf-8") == "evidence\n", "nothing may be overwritten"
    assert (release / ".release-manifest.json").read_text(encoding="utf-8") == before


def test_a_manifest_is_never_rewritten(tmp_path: Path, head_sha: str) -> None:
    root = tmp_path / "releases"
    root.mkdir()
    create(root, head_sha, "api")
    release = root / head_sha / "api"
    assert stamp(release, head_sha, "api").returncode == EXIT_OK
    original = (release / ".release-manifest.json").read_text(encoding="utf-8")
    again = stamp(release, head_sha, "api")
    assert again.returncode == EXIT_PRECONDITION
    assert "refusing to rewrite" in again.stderr
    assert (release / ".release-manifest.json").read_text(encoding="utf-8") == original


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_the_current_stage_is_named_in_the_refusal(tmp_path: Path, head_sha: str) -> None:
    """An operator must be told WHY, not just no."""
    root = tmp_path / "releases"
    root.mkdir()
    create(root, head_sha, "core")
    stamp(root / head_sha / "core", head_sha, "core")
    result = run_snippet(
        f'''
        point_symlink_atomic "{(root / 'current').as_posix()}" "{(root / head_sha / 'core').as_posix()}"
        create_release "{REPO_ROOT.as_posix()}" "{head_sha}" core "{root.as_posix()}"
        '''
    )
    assert result.returncode == EXIT_PRECONDITION
    assert "already the current release" in result.stderr
    assert "core" in result.stderr


# =============================================================================
# C. release_path validation
# =============================================================================


GOOD_SHA = "a1b2c3d4" * 5


def test_release_path_composes_commit_and_stage(tmp_path: Path) -> None:
    result = run_snippet(f'release_path "{tmp_path.as_posix()}" "{GOOD_SHA}" core')
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout == f"{tmp_path.as_posix()}/{GOOD_SHA}/core"


@pytest.mark.parametrize(
    "commit",
    ["main", "HEAD", "a1b2c3d", GOOD_SHA.upper(), "", "../../etc",
     "a1b2c3d4/../../../etc/passwd", "$(whoami)", "a1b2c3d4;rm -rf /"],
)
def test_release_path_refuses_anything_but_a_full_sha(commit: str) -> None:
    """The commit is a path component. A branch name, a traversal or a shell
    metacharacter reaching the filesystem would be a very bad day."""
    result = run_snippet(f'release_path /releases "{commit}" api')
    assert result.returncode != EXIT_OK, f"accepted {commit!r}"


@pytest.mark.parametrize("stage", ["", "API", "everything", "../core", "api core", "api;ls"])
def test_release_path_refuses_an_invalid_stage(stage: str) -> None:
    result = run_snippet(f'release_path /releases "{GOOD_SHA}" "{stage}"')
    assert result.returncode != EXIT_OK, f"accepted {stage!r}"


def test_release_path_refuses_a_newline_in_the_commit() -> None:
    """A newline would let one identity masquerade as two lines of output."""
    result = run_snippet(
        f'release_path /releases "$(printf \'{GOOD_SHA}\\napi\')" api'
    )
    assert result.returncode != EXIT_OK


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_a_symlinked_commit_directory_is_refused(tmp_path: Path, head_sha: str) -> None:
    """Otherwise a stage could be written outside the release root."""
    root = tmp_path / "releases"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / head_sha).symlink_to(elsewhere, target_is_directory=True)
    result = create(root, head_sha, "api")
    assert result.returncode != EXIT_OK
    assert "symlink" in result.stderr.lower()


# =============================================================================
# D. Release pointers carry commit AND stage
# =============================================================================


def fake_docker_bin(tmp_path: Path, present: list[str] | None = None) -> tuple[Path, Path]:
    """A docker stand-in that records argv, for build/pull assertions."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-calls.log"
    log.write_text("", encoding="utf-8")
    known = "\n".join(present or [])
    binary = bindir / "docker"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{log.as_posix()}"\n'
        'if [ "${1:-}" = "image" ] && [ "${2:-}" = "inspect" ]; then\n'
        '    for candidate in "$@"; do\n'
        '        while IFS= read -r item; do\n'
        '            [ -n "${item}" ] || continue\n'
        '            [ "${candidate}" = "${item}" ] && exit 0\n'
        "        done <<'IMAGES'\n"
        f"{known}\n"
        "IMAGES\n"
        "    done\n"
        "    exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, log


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_pointers_resolve_to_both_commit_and_stage(tmp_path: Path, head_sha: str) -> None:
    """`current` -> X/core and `previous` -> X/api must both be readable.

    Reading a pointer with basename now yields the STAGE, so any code that still
    treated it as the commit would compare "core" against a sha and silently
    conclude the release had changed.
    """
    root = tmp_path / "releases"
    root.mkdir()
    for stage in ("api", "core"):
        create(root, head_sha, stage)
        stamp(root / head_sha / stage, head_sha, stage)

    result = run_snippet(
        f"""
        point_symlink_atomic "{(root / 'current').as_posix()}" "{(root / head_sha / 'core').as_posix()}"
        point_symlink_atomic "{(root / 'previous').as_posix()}" "{(root / head_sha / 'api').as_posix()}"
        cur="$(read_release_identity "{(root / 'current').as_posix()}" "{root.as_posix()}")"
        prv="$(read_release_identity "{(root / 'previous').as_posix()}" "{root.as_posix()}")"
        printf 'current=%s/%s previous=%s/%s\\n' \\
            "$(release_identity_commit "${{cur}}")" "$(release_identity_stage "${{cur}}")" \\
            "$(release_identity_commit "${{prv}}")" "$(release_identity_stage "${{prv}}")"
        """
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert f"current={head_sha}/core" in result.stdout
    assert f"previous={head_sha}/api" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_a_pointer_outside_the_release_root_is_refused(tmp_path: Path, head_sha: str) -> None:
    """`current` is a thing a mistake can repoint, and everything downstream
    trusts what it says."""
    root = tmp_path / "releases"
    root.mkdir()
    outside = tmp_path / "outside" / head_sha / "full"
    outside.mkdir(parents=True)
    result = run_snippet(
        f"""
        point_symlink_atomic "{(root / 'current').as_posix()}" "{outside.as_posix()}"
        read_release_identity "{(root / 'current').as_posix()}" "{root.as_posix()}"
        """
    )
    assert result.returncode != EXIT_OK
    assert "outside" in result.stderr


# =============================================================================
# E. Manifest strictness
# =============================================================================


OTHER_SHA = "f9e8d7c6" * 5


def build_release(root: Path, commit: str, stage: str, **overrides) -> Path:
    release = root / commit / stage
    (release / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("VERSION", "compose.yaml", "compose.prod.yaml"):
        (release / name).write_text("x\n", encoding="utf-8")
    (release / "scripts" / "smoke-test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    document = {
        "schema_version": 1, "commit": commit, "stage": stage, "source": "git archive",
    }
    document.update(overrides)
    for key in [k for k, v in document.items() if v is None]:
        del document[key]
    (release / ".release-manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return release


def validate(release: Path, commit: str, stage: str):
    return run_snippet(
        f'validate_release_manifest "{release.as_posix()}" "{commit}" "{stage}"'
    )


@pytest.mark.parametrize("stage", STAGES)
def test_a_correct_manifest_is_accepted_for_every_stage(tmp_path: Path, stage: str) -> None:
    release = build_release(tmp_path, GOOD_SHA, stage)
    result = validate(release, GOOD_SHA, stage)
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout == stage


def test_a_missing_source_is_rejected(tmp_path: Path) -> None:
    """Absent used to PASS, so a hand-made directory with a plausible manifest
    and no provenance was indistinguishable from an archived release."""
    release = build_release(tmp_path, GOOD_SHA, "api", source=None)
    result = validate(release, GOOD_SHA, "api")
    assert result.returncode != EXIT_OK
    assert "git archive" in result.stderr


def test_an_empty_source_is_rejected(tmp_path: Path) -> None:
    release = build_release(tmp_path, GOOD_SHA, "api", source="")
    assert validate(release, GOOD_SHA, "api").returncode != EXIT_OK


def test_a_wrong_source_is_rejected(tmp_path: Path) -> None:
    release = build_release(tmp_path, GOOD_SHA, "api", source="rsync from a laptop")
    assert validate(release, GOOD_SHA, "api").returncode != EXIT_OK


def test_a_manifest_stage_mismatch_is_rejected(tmp_path: Path) -> None:
    """The manifest says core, but it sits in -- and was asked for as -- api."""
    release = build_release(tmp_path, GOOD_SHA, "api")
    (release / ".release-manifest.json").write_text(
        json.dumps({
            "schema_version": 1, "commit": GOOD_SHA,
            "stage": "core", "source": "git archive",
        }),
        encoding="utf-8",
    )
    result = validate(release, GOOD_SHA, "api")
    assert result.returncode != EXIT_OK
    assert "does not match the requested stage" in result.stderr


def test_a_directory_stage_mismatch_is_rejected(tmp_path: Path) -> None:
    """A manifest for core sitting in the api directory."""
    release = build_release(tmp_path, GOOD_SHA, "api")
    result = validate(release, GOOD_SHA, "core")
    assert result.returncode != EXIT_OK
    assert "does not match the requested stage" in result.stderr


def test_a_commit_parent_mismatch_is_rejected(tmp_path: Path) -> None:
    """A whole release tree moved under a different commit directory."""
    release = build_release(tmp_path, GOOD_SHA, "core")
    other = tmp_path / OTHER_SHA
    other.mkdir()
    release.rename(other / "core")
    result = validate(other / "core", GOOD_SHA, "core")
    assert result.returncode != EXIT_OK
    assert "does not match the requested commit" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_a_symlinked_commit_parent_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    build_release(real, GOOD_SHA, "api")
    links = tmp_path / "links"
    links.mkdir()
    (links / GOOD_SHA).symlink_to(real / GOOD_SHA, target_is_directory=True)
    result = validate(links / GOOD_SHA / "api", GOOD_SHA, "api")
    assert result.returncode != EXIT_OK
    assert "symlink" in result.stderr


# =============================================================================
# F/G. Rollback and recovery identity
# =============================================================================


def rollback(*args: str):
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, str(SCRIPTS / "rollback-compose.sh"), *args],
        capture_output=True, text=True, timeout=120, check=False,
        cwd=str(REPO_ROOT), env={**os.environ},
    )


def test_rollback_refuses_a_commit_without_a_stage() -> None:
    """A commit may exist at api, core and full. Guessing -- or defaulting to
    api, or picking the newest directory -- starts the wrong service set during
    an incident."""
    result = rollback("--to-commit", GOOD_SHA, "--dry-run")
    assert result.returncode == EXIT_USAGE
    assert "--stage" in result.stderr


def test_rollback_refuses_stage_combined_with_previous() -> None:
    """--previous carries its own recorded stage; a second opinion is a bug."""
    result = rollback("--previous", "--stage", "core", "--dry-run")
    assert result.returncode == EXIT_USAGE


def test_rollback_refuses_an_invalid_stage() -> None:
    result = rollback("--to-commit", GOOD_SHA, "--stage", "everything", "--dry-run")
    assert result.returncode == EXIT_USAGE


def test_rollback_never_guesses_a_stage() -> None:
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'TARGET_STAGE="api"' not in body, "must never default to api"
    assert "ls -t" not in body and "--sort" not in body, "must never pick by mtime"


def test_previous_identity_uses_both_recorded_fields() -> None:
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    assert 'read_state_field "${STATE_FILE}" previous_stage' in text
    assert "read_release_identity" in text, "fallback must read commit AND stage"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
@pytest.mark.parametrize(
    ("previous_stage", "attempted_stage"),
    [("api", "core"), ("core", "full"), ("full", "api")],
)
def test_same_commit_recovery_targets_the_previous_stage(
    tmp_path: Path, head_sha: str, previous_stage: str, attempted_stage: str
) -> None:
    """A failed `api X -> core X` must recover X/api.

    The commits are equal, so anything keyed on the commit alone would conclude
    there is no previous release and tear the stack down instead.
    """
    root = tmp_path / "releases"
    root.mkdir()
    create(root, head_sha, previous_stage)
    stamp(root / head_sha / previous_stage, head_sha, previous_stage)

    result = run_snippet(
        f"""
        RELEASE_ROOT="{root.as_posix()}"
        point_symlink_atomic "${{RELEASE_ROOT}}/current" \\
            "$(release_path "${{RELEASE_ROOT}}" "{head_sha}" "{previous_stage}")"
        identity="$(read_release_identity "${{RELEASE_ROOT}}/current" "${{RELEASE_ROOT}}")"
        commit="$(release_identity_commit "${{identity}}")"
        stage="$(release_identity_stage "${{identity}}")"
        if [ -z "${{commit}}" ] || [ -z "${{stage}}" ]; then
            echo NO_PREVIOUS_IDENTITY
            exit 1
        fi
        target="$(release_path "${{RELEASE_ROOT}}" "${{commit}}" "${{stage}}")"
        validate_release_manifest "${{target}}" "${{commit}}" "${{stage}}" >/dev/null
        printf 'recover=%s/%s services=%s\\n' "${{commit}}" "${{stage}}" \\
            "$(stage_plan "${{stage}}" runtime_services)"
        """
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "NO_PREVIOUS_IDENTITY" not in result.stdout
    assert f"recover={head_sha}/{previous_stage}" in result.stdout


def test_deploy_treats_a_same_commit_stage_change_as_having_a_previous_release() -> None:
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    assert 'PREVIOUS_STAGE=""' in text
    assert 'read_release_identity "${RELEASE_ROOT}/current"' in text
    # The first-deployment path must require a missing IDENTITY, not merely a
    # differing commit.
    assert '[ -z "${PREVIOUS_COMMIT}" ] || [ -z "${PREVIOUS_STAGE}" ]' in text


def test_recovery_validates_the_previous_stage_manifest() -> None:
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    body = text[text.index("restore_previous_release() {"):text.index("write_failure_report")]
    assert '"${PREVIOUS_COMMIT}" "${prev_stage}"' in body
    assert "--no-build" in body and "--pull never" in body


# =============================================================================
# H. Image reuse during promotion
# =============================================================================


@pytest.mark.parametrize(
    ("stage", "present", "expected"),
    [
        ("api", [], "api"),
        ("api", ["radio-api:" + GOOD_SHA], ""),
        ("core", ["radio-api:" + GOOD_SHA], "planner"),
        ("core", [], "api planner"),
        ("full", ["radio-api:" + GOOD_SHA, "radio-pipeline:" + GOOD_SHA], "llm"),
        ("full", [], "api planner llm"),
    ],
)
def test_only_missing_images_are_built(
    tmp_path: Path, stage: str, present: list[str], expected: str
) -> None:
    """Promoting api X -> core X must not rebuild radio-api:X.

    It exists, it is byte-identical because the commit is identical, and
    rebuilding it would mint a new image id for the same source -- making the
    deployment history look like the API changed when it did not.
    """
    bindir, _ = fake_docker_bin(tmp_path, present)
    result = run_snippet(
        f'missing_stage_build_services "{stage}" "{GOOD_SHA}"', path_prefix=bindir
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout.split() == expected.split()


def test_images_are_not_tagged_per_stage() -> None:
    """The source is identical across stages, so there is no radio-api:X-core."""
    for stage in STAGES:
        result = run_snippet(f'stage_required_images "{stage}" "{GOOD_SHA}"')
        for image in result.stdout.split():
            assert image.endswith(":" + GOOD_SHA), f"{image} is not tagged by commit alone"


def test_state_records_both_commit_and_stage_on_each_side() -> None:
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    for field in ("current_commit", "current_stage", "previous_commit", "previous_stage"):
        assert f'"{field}":' in text, f"state must record {field}"
    assert "legacy alias of current_stage" in text, "the authoritative field must be documented"


def test_failed_history_records_attempted_and_previous_stages() -> None:
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    for field in ("attempted_commit", "attempted_stage", "previous_commit", "previous_stage"):
        assert f'"{field}":' in text, f"failure history must record {field}"
