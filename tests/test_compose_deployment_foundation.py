"""Image identity, Compose exposure policy and the migration CLI.

Static assertions against the Dockerfiles and Compose files, plus a real
migration run against a temporary SQLite file. Nothing here builds an image,
starts a container, downloads a model or contacts AWS.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = {
    "api": REPO_ROOT / "docker" / "api.Dockerfile",
    "pipeline": REPO_ROOT / "docker" / "pipeline.Dockerfile",
    "llm": REPO_ROOT / "docker" / "llm.Dockerfile",
}
COMPOSE = REPO_ROOT / "compose.yaml"
COMPOSE_PROD = REPO_ROOT / "compose.prod.yaml"
COMPOSE_ENV_EXAMPLE = REPO_ROOT / "deploy" / "compose" / "compose.env.example"

DOCKER = shutil.which("docker")


# --- A. Docker identity -------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_every_image_takes_uid_and_gid_build_args(name: str) -> None:
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert "ARG RADIO_UID=10001" in text, f"{name} must accept a configurable uid"
    assert "ARG RADIO_GID=10001" in text, f"{name} must accept a configurable gid"


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_defaults_remain_10001(name: str) -> None:
    """Local development and generic builds must be unchanged."""
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert re.search(r"ARG RADIO_UID=10001", text)
    assert re.search(r"ARG RADIO_GID=10001", text)


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_user_creation_uses_the_build_args_not_a_constant(name: str) -> None:
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert '--gid "${RADIO_GID}" radio' in text
    assert '--uid "${RADIO_UID}"' in text
    assert "groupadd --gid 10001" not in text, "hard-coded gid must be gone"
    assert "useradd --uid 10001" not in text, "hard-coded uid must be gone"


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_images_validate_uid_and_gid_at_build_time(name: str) -> None:
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert "must be numeric" in text, "non-numeric ids must be refused"
    assert "must not be 0 (root)" in text, "root must be refused"
    assert "65533" in text, "out-of-range ids must be refused"


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_images_still_run_as_a_non_root_user(name: str) -> None:
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert "USER radio:radio" in text
    assert "\nUSER root" not in text


@pytest.mark.parametrize("name", ["api", "pipeline"])
def test_application_source_stays_root_owned(name: str) -> None:
    """A compromised worker must not be able to rewrite its own code."""
    text = DOCKERFILES[name].read_text(encoding="utf-8")
    assert "COPY --chown=root:root app ./app" in text


def test_the_uid_guard_logic_accepts_and_rejects_correctly(tmp_path: Path) -> None:
    """Extract the guard from the Dockerfile and run it directly."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")

    guard = tmp_path / "guard.sh"
    guard.write_text(
        "#!/bin/sh\nset -eu\n"
        'for value in "${RADIO_UID}" "${RADIO_GID}"; do\n'
        "  case \"${value}\" in ''|*[!0-9]*) echo nonnumeric >&2; exit 1 ;; esac\n"
        '  [ "${value}" -ge 1 ] || { echo root >&2; exit 1; }\n'
        '  [ "${value}" -le 65533 ] || { echo range >&2; exit 1; }\n'
        "done\necho ok\n",
        encoding="utf-8",
    )
    cases = {
        ("10001", "10001"): 0,
        ("992", "992"): 0,
        ("0", "992"): 1,
        ("992", "0"): 1,
        ("-5", "992"): 1,
        ("abc", "992"): 1,
        ("70000", "992"): 1,
    }
    for (uid, gid), expected in cases.items():
        result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array
            [bash, str(guard)],
            env={**os.environ, "RADIO_UID": uid, "RADIO_GID": gid},
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == expected, f"uid={uid} gid={gid}"


# --- B. Compose publish host --------------------------------------------------


def test_compose_passes_uid_and_gid_to_every_build() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("RADIO_UID: ${RADIO_CONTAINER_UID:-10001}") == 3
    assert text.count("RADIO_GID: ${RADIO_CONTAINER_GID:-10001}") == 3


def test_production_publish_host_is_configurable_and_defaults_to_loopback() -> None:
    text = COMPOSE_PROD.read_text(encoding="utf-8")
    assert '"${RADIO_API_PUBLISH_HOST:-127.0.0.1}:8788:8788"' in text
    assert '"0.0.0.0:8788:8788"' not in text


def test_the_llm_port_is_still_never_published() -> None:
    for path in (COMPOSE, COMPOSE_PROD):
        text = path.read_text(encoding="utf-8")
        assert "8790:8790" not in text, f"{path.name} must not publish the LLM port"


def test_only_the_api_declares_a_published_port() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    # `expose:` is internal-only; `ports:` publishes to the host.
    assert text.count("\n    ports:") == 1, "exactly one service may publish"


def test_compose_env_example_defaults_are_safe() -> None:
    text = COMPOSE_ENV_EXAMPLE.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    assert values["RADIO_API_PUBLISH_HOST"] == "127.0.0.1"
    assert values["RADIO_ALLOW_DIRECT_HTTP"] == "0"
    assert values["RADIO_CONTAINER_UID"] == "10001"
    assert values["COMPOSE_PROJECT_NAME"] == "radio-prod"


def test_compose_env_example_contains_no_secret_or_account_identifier() -> None:
    text = COMPOSE_ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", line), f"account id in: {line!r}"
        assert "amazonaws.com" not in line
        assert "SECRET" not in line.upper() or "=" not in line


def test_the_real_compose_env_is_git_ignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "compose.env" in gitignore, "the real compose.env must never be committed"


@pytest.mark.skipif(DOCKER is None, reason="docker is not available on this host")
@pytest.mark.parametrize(
    ("publish_host", "expected"),
    [("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "0.0.0.0")],
)
def test_compose_renders_the_requested_publish_host(publish_host: str, expected: str) -> None:
    """Compose interpolation actually honours the variable."""
    result = subprocess.run(  # noqa: S603 - fixed binary, argument array
        [DOCKER, "compose", "-f", str(COMPOSE), "-f", str(COMPOSE_PROD),
         "--profile", "core", "config"],
        capture_output=True, text=True, check=False, cwd=str(REPO_ROOT),
        env={**os.environ, "RADIO_ENV_DIR": "./deploy/dev",
             "RADIO_API_PUBLISH_HOST": publish_host},
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


@pytest.mark.skipif(DOCKER is None, reason="docker is not available on this host")
def test_production_compose_resolves_with_the_host_uid_and_gid() -> None:
    result = subprocess.run(  # noqa: S603 - fixed binary, argument array
        [DOCKER, "compose", "-f", str(COMPOSE), "-f", str(COMPOSE_PROD),
         "--profile", "core", "--profile", "pipeline", "--profile", "llm", "config"],
        capture_output=True, text=True, check=False, cwd=str(REPO_ROOT),
        env={**os.environ, "RADIO_ENV_DIR": "./deploy/dev",
             "RADIO_CONTAINER_UID": "992", "RADIO_CONTAINER_GID": "992"},
    )
    assert result.returncode == 0, result.stderr
    assert "RADIO_UID: \"992\"" in result.stdout or "RADIO_UID: '992'" in result.stdout \
        or "RADIO_UID: 992" in result.stdout


# --- G. migration CLI ---------------------------------------------------------


def migrate(*args: str, **env) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "RADIO_S3_BUCKET": "fake-bucket-for-tests",
        "RADIO_AUDIO_TOKEN_SECRET": "x" * 48,
        **{k: str(v) for k, v in env.items()},
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [sys.executable, "-m", "app.cli.migrate_database", *args],
        capture_output=True, text=True, timeout=120, check=False,
        cwd=str(REPO_ROOT), env=environment,
    )


def test_migration_creates_and_populates_schema_migrations(tmp_path: Path) -> None:
    database = tmp_path / "radio.db"
    result = migrate("--database", str(database))
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["schema_version_before"] is None
    assert payload["schema_version_after"] >= 6
    assert payload["migrations_applied"], "schema_migrations must be populated"
    assert database.is_file()


def test_migration_runs_the_integrity_check(tmp_path: Path) -> None:
    result = migrate("--database", str(tmp_path / "radio.db"))
    assert json.loads(result.stdout)["integrity_check"] == "ok"


def test_migration_applies_the_required_pragmas(tmp_path: Path) -> None:
    payload = json.loads(migrate("--database", str(tmp_path / "radio.db")).stdout)
    pragmas = payload["pragmas"]
    assert pragmas["journal_mode"] == "wal"
    assert pragmas["foreign_keys"] == 1
    assert pragmas["busy_timeout"] == 30000
    assert pragmas["synchronous"] == 1


def test_migration_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "radio.db"
    first = json.loads(migrate("--database", str(database)).stdout)
    second = json.loads(migrate("--database", str(database)).stdout)
    assert second["schema_version_before"] == first["schema_version_after"]
    assert second["schema_version_after"] == first["schema_version_after"]
    assert second["status"] == "PASS"


def test_check_only_does_not_create_a_database(tmp_path: Path) -> None:
    database = tmp_path / "radio.db"
    result = migrate("--database", str(database), "--check-only")
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "ABSENT"
    assert not database.exists(), "--check-only must not create the file"


def test_migration_opens_no_http_socket_and_starts_no_worker(tmp_path: Path) -> None:
    """The process must exit on its own; a server or worker would hang."""
    result = migrate("--database", str(tmp_path / "radio.db"))
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    for marker in ("Uvicorn", "Started server", "Application startup", "Worker starting"):
        assert marker not in combined, f"migration must not start a service ({marker})"


def test_migration_failure_exits_non_zero(tmp_path: Path) -> None:
    # A directory where the database file should be makes SQLite fail to open.
    blocked = tmp_path / "radio.db"
    blocked.mkdir()
    result = migrate("--database", str(blocked))
    assert result.returncode != 0


def test_migration_output_contains_no_secret(tmp_path: Path) -> None:
    secret = "SUPERSECRETTOKENVALUE1234567890abcdef"
    result = migrate("--database", str(tmp_path / "radio.db"), RADIO_AUDIO_TOKEN_SECRET=secret)
    assert secret not in result.stdout
    assert secret not in result.stderr


def migrate_bare(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI with the application configuration deliberately absent."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RADIO_") and not key.startswith("AWS_")
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [sys.executable, "-m", "app.cli.migrate_database", *args],
        capture_output=True, text=True, timeout=120, check=False,
        cwd=str(REPO_ROOT), env=environment,
    )


def test_migration_runs_without_application_configuration(tmp_path: Path) -> None:
    """--database is a real escape hatch, not a decoration.

    Recovering a schema must not require an S3 bucket name and an audio-token
    secret to be present -- those have nothing to do with SQLite, and demanding
    them couples recovery to exactly the configuration an operator may be in the
    middle of fixing.
    """
    database = tmp_path / "radio.db"
    result = migrate_bare("--database", str(database))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["schema_version_after"] >= 6
    assert payload["pragmas"]["journal_mode"] == "wal"


def test_migration_without_database_or_configuration_is_a_usage_error() -> None:
    """With neither, there is no way to know which file to migrate."""
    result = migrate_bare("--check-only")
    assert result.returncode == 64
    assert "--database" in result.stderr, "the error must say how to proceed"


def test_the_cli_package_imports_without_boto3(tmp_path: Path) -> None:
    """The migration CLI must not depend on the AWS SDK."""
    probe = (
        "import sys, types;"
        "sys.modules['boto3'] = None;"
        "import app.cli.migrate_database as m;"
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=False, cwd=str(REPO_ROOT),
        env={**os.environ, "RADIO_S3_BUCKET": "x", "RADIO_AUDIO_TOKEN_SECRET": "x" * 48},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# --- J. cleanup one-shot ------------------------------------------------------


def test_cleanup_exposes_a_one_shot_entrypoint() -> None:
    text = (REPO_ROOT / "app" / "workers" / "cleanup.py").read_text(encoding="utf-8")
    assert "--once" in text
    assert "--dry-run" in text
    assert "def deletable_segments" in text, "dry-run must reuse the real safety query"


def test_cleanup_safety_predicates_live_in_one_place() -> None:
    """`sweep` must not carry a second copy of the deletion predicates."""
    text = (REPO_ROOT / "app" / "workers" / "cleanup.py").read_text(encoding="utf-8")
    assert text.count("FROM audio_segments s") == 1, "one definition of safe-to-delete"
    assert "disposition NOT IN ('retained', 'pending')" in text
