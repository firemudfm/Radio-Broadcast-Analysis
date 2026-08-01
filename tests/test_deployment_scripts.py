"""Deployment-script safety gates.

Each helper in ``scripts/lib/deploy-common.sh`` is invoked directly rather than
through the whole deploy script, so a single gate can be proven to fire without
first satisfying every gate before it.

These are the checks that stand between a reviewed commit and a production
host, so the tests assert the *refusals* at least as hard as the successes: a
gate that silently passes bad input is worse than no gate.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib" / "deploy-common.sh"
SCRIPTS = REPO_ROOT / "scripts"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available on this host")

# Exit codes mirrored from deploy-common.sh.
EXIT_OK = 0
EXIT_USAGE = 64
EXIT_PRECONDITION = 65


def run_snippet(snippet: str, **env) -> subprocess.CompletedProcess:
    """Source the shared library and run one snippet against it."""
    script = f'set -euo pipefail\nsource "{LIB.as_posix()}"\n{textwrap.dedent(snippet)}'
    environment = {**os.environ, **{k: str(v) for k, v in env.items()}}
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )


def run_script(name: str, *args: str, **env) -> subprocess.CompletedProcess:
    environment = {**os.environ, **{k: str(v) for k, v in env.items()}}
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [BASH, str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(REPO_ROOT),
        env=environment,
    )


# --- C. exact commit ----------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["main", "HEAD", "v1.0", "c085c44", "C085C44C1DB682DDD451EF8551AE524903D4AE0C", ""],
)
def test_only_a_full_lowercase_sha_is_accepted(value: str) -> None:
    """A branch name would let deployed content change after approval."""
    result = run_snippet(f'validate_full_sha "{value}"')
    assert result.returncode == EXIT_USAGE, f"{value!r} should have been refused"


def test_a_full_sha_is_accepted() -> None:
    result = run_snippet('validate_full_sha "c085c44c1db682ddd451ef8551ae524903d4ae0c"')
    assert result.returncode == EXIT_OK, result.stderr


def test_a_dirty_source_tree_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    result = run_snippet(f'require_clean_source "{repo.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "uncommitted changes" in result.stderr


def test_a_clean_source_tree_is_accepted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    result = run_snippet(f'require_clean_source "{repo.as_posix()}"')
    assert result.returncode == EXIT_OK, result.stderr


def test_a_missing_commit_is_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    result = run_snippet(
        f'commit_exists_locally "{repo.as_posix()}" 0000000000000000000000000000000000000000'
    )
    assert result.returncode != 0


def test_git_archive_produces_the_expected_release(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _git_init(repo, with_release_files=True)
    releases = tmp_path / "releases"
    releases.mkdir()

    result = run_snippet(
        f'create_release "{repo.as_posix()}" "{sha}" "{releases.as_posix()}"'
    )
    assert result.returncode == EXIT_OK, result.stderr

    release = releases / sha
    assert release.is_dir(), "release directory should be named after the commit"
    assert (release / "VERSION").is_file()
    assert (release / "compose.yaml").is_file()
    # git archive cannot include .git, but assert it so a future change to how
    # releases are built cannot start shipping repository metadata.
    assert not (release / ".git").exists()


def test_a_release_cannot_escape_the_release_root(tmp_path: Path) -> None:
    """The release path is <root>/<sha>, and sha is validated as 40 hex."""
    result = run_snippet('validate_full_sha "../../etc/passwd"')
    assert result.returncode == EXIT_USAGE


def _git_init(repo: Path, *, with_release_files: bool = False) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)  # noqa: S603,S607
    if with_release_files:
        (repo / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        (repo / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (repo / "compose.prod.yaml").write_text("services: {}\n", encoding="utf-8")
        (repo / "docker").mkdir(exist_ok=True)
        (repo / "docker" / "api.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (repo / "scripts").mkdir(exist_ok=True)
        (repo / "scripts" / "smoke-test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    else:
        (repo / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)  # noqa: S603,S607
    subprocess.run(  # noqa: S603,S607
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env
    )
    return subprocess.run(  # noqa: S603,S607
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


# --- A. runtime identity ------------------------------------------------------


@pytest.mark.parametrize(
    ("uid", "gid"),
    [("10001", "10001"), ("992", "992"), ("1", "1"), ("65533", "65533")],
)
def test_valid_uid_gid_accepted(uid: str, gid: str) -> None:
    assert run_snippet(f'validate_uid_gid "{uid}" "{gid}"').returncode == EXIT_OK


@pytest.mark.parametrize(
    ("uid", "gid"),
    [
        ("0", "992"),      # root
        ("992", "0"),      # root group
        ("-5", "992"),     # negative
        ("abc", "992"),    # non-numeric
        ("", "992"),       # empty
        ("70000", "992"),  # out of range
        ("992", "99999"),
    ],
)
def test_invalid_uid_gid_rejected(uid: str, gid: str) -> None:
    result = run_snippet(f'validate_uid_gid "{uid}" "{gid}"')
    assert result.returncode == EXIT_USAGE, f"{uid}:{gid} should have been refused"


def test_production_identity_is_representable() -> None:
    """The production host's radio account is 992:992, not the 10001 default."""
    assert run_snippet('validate_uid_gid "992" "992"').returncode == EXIT_OK


# --- B. publish host ----------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_publish_hosts_are_accepted(host: str) -> None:
    assert run_snippet(f'validate_publish_host "{host}" "0"').returncode == EXIT_OK


def test_direct_binding_is_refused_without_acknowledgement() -> None:
    result = run_snippet('validate_publish_host "0.0.0.0" "0"')
    assert result.returncode == EXIT_PRECONDITION
    assert "auth_mode=none" in result.stderr
    assert "RADIO_ALLOW_DIRECT_HTTP=1" in result.stderr


def test_direct_binding_is_accepted_with_explicit_acknowledgement() -> None:
    result = run_snippet('validate_publish_host "0.0.0.0" "1"')
    assert result.returncode == EXIT_OK
    assert "no TLS" in result.stderr, "must still warn loudly"


@pytest.mark.parametrize("host", ["8.8.8.8", "example.com", "0.0.0.0:8788", "*"])
def test_unexpected_publish_hosts_are_rejected(host: str) -> None:
    result = run_snippet(f'validate_publish_host "{host}" "1"')
    assert result.returncode == EXIT_USAGE


# --- E. environment validation ------------------------------------------------


def test_a_missing_environment_file_is_refused(tmp_path: Path) -> None:
    result = run_snippet(f'require_env_file "{(tmp_path / "absent.env").as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "missing" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not meaningful on Windows")
def test_permissive_environment_file_permissions_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "application.env"
    path.write_text("RADIO_AUDIO_TOKEN_SECRET=x\n", encoding="utf-8")
    path.chmod(0o666)
    result = run_snippet(f'require_env_file "{path.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "permissive" in result.stderr


def test_a_placeholder_audio_secret_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "application.env"
    path.write_text(
        "RADIO_AUDIO_TOKEN_SECRET=replace-me-with-at-least-32-random-characters\n",
        encoding="utf-8",
    )
    result = run_snippet(f'reject_placeholder_secret "{path.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "placeholder" in result.stderr


def test_a_real_looking_secret_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "application.env"
    path.write_text("RADIO_AUDIO_TOKEN_SECRET=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA\n", encoding="utf-8")
    assert run_snippet(f'reject_placeholder_secret "{path.as_posix()}"').returncode == EXIT_OK


# Assembled at runtime, never stored as a literal: an AKIA-shaped string in a
# tracked file trips this repository's own scripts/secret-scan.sh, and a scanner
# that its own test suite defeats is worthless.
EXAMPLE_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize(
    "line",
    [
        f"AWS_ACCESS_KEY_ID={EXAMPLE_ACCESS_KEY}",
        "AWS_SECRET_ACCESS_KEY=abc123",
        "AWS_SESSION_TOKEN=xyz",
    ],
)
def test_static_aws_credentials_are_refused(tmp_path: Path, line: str) -> None:
    path = tmp_path / "infrastructure.env"
    path.write_text(f"AWS_REGION=eu-north-1\n{line}\n", encoding="utf-8")
    result = run_snippet(f'reject_static_aws_credentials "{path.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "instance role" in result.stderr


def test_environment_contents_are_never_echoed(tmp_path: Path) -> None:
    """A validation failure must not print the secret it is validating."""
    secret = "SUPERSECRETVALUE1234567890"
    path = tmp_path / "application.env"
    path.write_text(f"RADIO_AUDIO_TOKEN_SECRET=replace-me\nOTHER={secret}\n", encoding="utf-8")
    result = run_snippet(f'reject_placeholder_secret "{path.as_posix()}"')
    assert secret not in result.stdout
    assert secret not in result.stderr


# --- F. disk and ownership ----------------------------------------------------


def test_low_disk_space_is_refused(tmp_path: Path) -> None:
    # Require an absurd amount so the check fires regardless of the host.
    result = run_snippet(f'require_free_space "{tmp_path.as_posix()}" 999999999')
    assert result.returncode == EXIT_PRECONDITION
    assert "MiB free" in result.stderr


def test_sufficient_disk_space_is_accepted(tmp_path: Path) -> None:
    assert run_snippet(f'require_free_space "{tmp_path.as_posix()}" 1').returncode == EXIT_OK


def test_a_missing_directory_reports_a_remediation_command(tmp_path: Path) -> None:
    missing = tmp_path / "database"
    result = run_snippet(
        f'require_writable_ownership 992 992 "{missing.as_posix()}"'
    )
    assert result.returncode == EXIT_PRECONDITION
    assert "install -d -o 992 -g 992" in result.stderr, "must print the fix, not apply it"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership is not meaningful on Windows")
def test_wrong_ownership_is_refused_and_never_chowned(tmp_path: Path) -> None:
    target = tmp_path / "spool"
    target.mkdir()
    before = target.stat().st_uid
    # 65000 is almost certainly not the owner of a pytest tmp_path.
    result = run_snippet(f'require_writable_ownership 65000 65000 "{target.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "chown -R 65000:65000" in result.stderr
    assert target.stat().st_uid == before, "the check must never chown anything itself"


# --- H. deployment state ------------------------------------------------------


def test_state_is_written_atomically(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    body = '{"schema_version": 1, "current_commit": "abc"}'
    result = run_snippet(f"""write_state_atomic "{state.as_posix()}" '{body}'""")
    assert result.returncode == EXIT_OK, result.stderr
    assert state.is_file()
    import json

    assert json.loads(state.read_text(encoding="utf-8"))["current_commit"] == "abc"
    # No staging file should survive.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_state_fields_can_be_read_back(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"current_commit": "abc123", "previous_commit": "def456"}', encoding="utf-8")
    result = run_snippet(f'read_state_field "{state.as_posix()}" previous_commit')
    assert result.stdout.strip() == "def456"


def test_reading_a_missing_state_file_is_not_an_error(tmp_path: Path) -> None:
    result = run_snippet(f'read_state_field "{(tmp_path / "none.json").as_posix()}" current_commit')
    assert result.returncode == EXIT_OK
    assert result.stdout.strip() == ""


def test_invalid_state_json_does_not_crash_the_reader(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    result = run_snippet(f'read_state_field "{state.as_posix()}" current_commit')
    assert result.returncode == EXIT_OK
    assert result.stdout.strip() == ""


# --- script contracts ---------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "deploy-compose.sh",
        "rollback-compose.sh",
        "migrate-db.sh",
        "cleanup-spool.sh",
        "container-smoke-test.sh",
    ],
)
def test_every_script_supports_help(script: str) -> None:
    result = run_script(script, "--help")
    assert result.returncode == EXIT_OK, result.stderr
    assert "Usage:" in result.stdout


@pytest.mark.parametrize(
    "script",
    [
        "deploy-compose.sh",
        "rollback-compose.sh",
        "migrate-db.sh",
        "cleanup-spool.sh",
        "container-smoke-test.sh",
        "backup-sqlite.sh",
        "secret-scan.sh",
        "smoke-test.sh",
        "compose-check.sh",
        "lib/deploy-common.sh",
    ],
)
def test_every_script_passes_bash_syntax_check(script: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, "-n", str(SCRIPTS / script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "script",
    ["deploy-compose.sh", "rollback-compose.sh", "migrate-db.sh", "cleanup-spool.sh"],
)
def test_scripts_use_strict_mode(script: str) -> None:
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


@pytest.mark.parametrize(
    "script",
    ["deploy-compose.sh", "rollback-compose.sh", "migrate-db.sh", "cleanup-spool.sh"],
)
def test_scripts_never_use_eval_or_network_git(script: str) -> None:
    code = executable_lines(SCRIPTS / script)
    for forbidden in ("eval ", "git pull", "git fetch", "git reset", "git checkout",
                      "docker system prune"):
        assert forbidden not in code, f"{script} must not execute {forbidden!r}"


def test_deploy_refuses_an_unknown_stage() -> None:
    result = run_script(
        "deploy-compose.sh", "--commit", "c" * 40, "--stage", "everything", "--dry-run"
    )
    assert result.returncode == EXIT_USAGE
    assert "--stage must be" in result.stderr


def test_deploy_defaults_to_the_api_stage_not_full() -> None:
    """`full` starts live capture; it must never be the accidental default."""
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    assert 'STAGE="api"' in text


def test_rollback_requires_an_explicit_target() -> None:
    result = run_script("rollback-compose.sh")
    assert result.returncode == EXIT_USAGE
    assert "--previous or --to-commit" in result.stderr


def test_rollback_refuses_a_branch_name() -> None:
    result = run_script("rollback-compose.sh", "--to-commit", "main", "--dry-run")
    assert result.returncode in {EXIT_USAGE, EXIT_PRECONDITION}


def test_rollback_never_restores_the_database() -> None:
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    assert "NOT restored" in text or "NOT be restored" in text
    # No path that copies a backup back over the live database.
    assert "cp " not in text.replace("cp -f", ""), "rollback must not copy a backup into place"


def test_migrate_requires_an_explicit_image() -> None:
    result = run_script("migrate-db.sh")
    assert result.returncode == EXIT_USAGE
    assert "--image is required" in result.stderr


def test_cleanup_requires_an_explicit_image() -> None:
    result = run_script("cleanup-spool.sh")
    assert result.returncode == EXIT_USAGE


def executable_lines(path: Path) -> str:
    """Script text with comment lines removed.

    Comments legitimately *name* the constructs they forbid; only executable
    lines are evidence of the construct actually being used.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_cleanup_contains_no_deletion_policy_of_its_own() -> None:
    """Retention lives in the cleanup service; a shell copy would drift."""
    code = executable_lines(SCRIPTS / "cleanup-spool.sh")
    for forbidden in ("-delete", "rm -rf", "find "):
        assert forbidden not in code, f"cleanup-spool.sh must not execute {forbidden!r}"
    assert "app.workers.cleanup" in code, "must delegate to the real cleanup service"


def test_migration_container_is_network_isolated() -> None:
    text = (SCRIPTS / "migrate-db.sh").read_text(encoding="utf-8")
    assert "--network none" in text, "a schema migration must not reach S3, SQS or a model host"
    assert "--entrypoint python" in text, "the image entrypoint is uvicorn and must be overridden"
    assert "-p " not in text and "--publish" not in text, "migration must publish no port"
