"""Regressions for the deployment safety gaps closed in PR #6.

Every test here corresponds to a defect that was present and demonstrable:

* workers waited for API *health*, which could not happen until workers ran;
* the post-deploy smoke test asserted a route that has never existed;
* a release directory was trusted because its NAME matched the approved sha;
* the public compose template pinned a uid the documented host does not use;
* a container with no healthcheck counted as healthy;
* a failed first deployment left half-started services running.

These assert the refusals harder than the successes. A gate that cannot fail is
indistinguishable from no gate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
LIB = SCRIPTS / "lib" / "deploy-common.sh"
COMPOSE = REPO_ROOT / "compose.yaml"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "compose" / "compose.env.example"

BASH = shutil.which("bash")
DOCKER = shutil.which("docker")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available on this host")

EXIT_OK = 0
EXIT_PRECONDITION = 65
EXIT_HEALTH = 72


def run_snippet(snippet: str, **env) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nsource "{LIB.as_posix()}"\n{textwrap.dedent(snippet)}'
    environment = {**os.environ, **{k: str(v) for k, v in env.items()}}
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [BASH, "-c", script],
        capture_output=True, text=True, timeout=120, check=False, env=environment,
    )


# =============================================================================
# 2. The API/worker health dependency cycle
# =============================================================================


def _compose_executable_text() -> str:
    """compose.yaml with comment lines removed.

    The comments deliberately NAME the condition they forbid, so scanning raw
    text would match the explanation rather than a real dependency edge.
    """
    return "\n".join(
        line for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_no_worker_waits_for_api_health() -> None:
    """The deadlock: workers waited for /readyz, which waits for workers.

    api healthcheck -> /readyz -> (shared_sqs) planner+listener+transcription+
    analysis heartbeats -> written only by workers -> which were waiting for the
    api to be healthy. The full stage could never start.
    """
    text = _compose_executable_text()
    assert "service_healthy" not in text, (
        "a service waits for another to be healthy; the API cannot become "
        "healthy until the workers it would block are already running"
    )


def test_workers_still_start_after_the_api() -> None:
    """Ordering is kept even though readiness is not.

    The API applies the schema migrations in its lifespan. Dropping the
    dependency entirely would let a worker open the database first and race it.
    """
    text = _compose_executable_text()
    assert re.search(
        r"depends_on:\s*\n\s+api:\s*\n\s+condition:\s+service_started", text
    ), "workers must still start after the API, just not wait for its health"


@pytest.mark.skipif(DOCKER is None, reason="docker is not available on this host")
def test_full_shared_sqs_stack_has_no_startup_cycle(tmp_path: Path) -> None:
    """Resolve the real full stack and walk the dependency graph."""
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    (env_dir / "infrastructure.env").write_text("AWS_REGION=eu-north-1\nRADIO_S3_BUCKET=b\n")
    (env_dir / "application.env").write_text("RADIO_AUDIO_TOKEN_SECRET=" + "z" * 48 + "\n")

    rendered = subprocess.run(  # noqa: S603 - fixed argument array
        [DOCKER, "compose", "-f", str(REPO_ROOT / "compose.yaml"),
         "-f", str(REPO_ROOT / "compose.prod.yaml"),
         "--profile", "core", "--profile", "pipeline", "--profile", "llm",
         "config", "--format", "json"],
        capture_output=True, text=True, timeout=180, check=False, cwd=str(REPO_ROOT),
        env={**os.environ,
             "RADIO_ENV_DIR": env_dir.as_posix(),
             "RADIO_CONTAINER_UID": "992", "RADIO_CONTAINER_GID": "992",
             "RADIO_API_PUBLISH_HOST": "127.0.0.1"},
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]

    # A readiness edge is one that blocks until the target is *healthy*. Those
    # are the only edges that can deadlock; service_started edges cannot.
    readiness_edges = {
        name: [
            target
            for target, spec in (block.get("depends_on") or {}).items()
            if isinstance(spec, dict) and spec.get("condition") == "service_healthy"
        ]
        for name, block in services.items()
    }

    # The API is the one service whose health depends on other services doing
    # work, so nothing may block on it.
    blocked_on_api = [name for name, targets in readiness_edges.items() if "api" in targets]
    assert not blocked_on_api, f"{blocked_on_api} block on API health in the full stack"

    # And no readiness cycle of any shape.
    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        for nxt in readiness_edges.get(start, []):
            if nxt == goal or (nxt not in seen and reaches(nxt, goal, seen | {nxt})):
                return True
        return False

    cycles = [name for name in services if reaches(name, name, {name})]
    assert not cycles, f"readiness dependency cycle involving {cycles}"


def test_api_readiness_is_false_while_workers_are_still_starting() -> None:
    """Proves the cycle was real, not theoretical.

    In shared_sqs mode readiness requires every worker heartbeat. Before the
    workers run there are none, so the API is legitimately unready -- which is
    exactly why nothing may wait for its health to start.
    """
    from app.services.pipeline_status import REQUIRED_ROLES

    assert REQUIRED_ROLES, "shared_sqs readiness must require worker heartbeats"
    source = (REPO_ROOT / "app" / "services" / "pipeline_status.py").read_text(encoding="utf-8")
    assert "all(checks[role] == \"ok\" for role in REQUIRED_ROLES)" in source, (
        "readiness must still require every worker role; it was not weakened to "
        "make the dependency cycle disappear"
    )


def test_full_stage_smoke_still_requires_every_worker_heartbeat() -> None:
    """Readiness was not weakened -- the final gate still checks all of it."""
    smoke = (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8")
    for component in ("listener", "transcription_worker", "analysis_worker", "planner"):
        assert component in smoke, f"smoke test must still assert {component}"
    assert "/api/v1/monitoring/pipeline" in smoke


# =============================================================================
# 3. The post-deployment API contract check
# =============================================================================


def test_smoke_test_does_not_use_the_route_that_never_existed() -> None:
    """/api/v1/campaigns 404s on every healthy deployment; the prefix is
    /api/v1/brand-signal."""
    executable = "\n".join(
        line for line in (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "/api/v1/campaigns" not in executable


def test_smoke_test_asserts_the_real_campaign_route() -> None:
    smoke = (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8")
    assert "/api/v1/brand-signal/campaigns" in smoke
    assert "/api/v1/brand-signal/mentions" in smoke


class _FakeApi(BaseHTTPRequestHandler):
    """Serves just enough for the smoke test, with a controllable route table."""

    paths: list[str] = []

    def log_message(self, *args) -> None:  # noqa: A003 - silence the test server
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        bodies = {
            "/healthz": {"status": "ok", "database": "ok", "auth_mode": "none",
                         "s3": "ok", "llm": "disabled", "pipeline_mode": "legacy"},
            "/readyz": {"ready": True, "pipeline_mode": "legacy", "checks": {"database": "ok"}},
            "/openapi.json": {"paths": {p: {} for p in self.paths}},
        }
        if self.path not in bodies:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(bodies[self.path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve(paths: list[str]):
    handler = type("Handler", (_FakeApi,), {"paths": paths})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    return server, f"http://127.0.0.1:{port}"


def _run_smoke(base_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, str(SCRIPTS / "smoke-test.sh"), base_url],
        capture_output=True, text=True, timeout=120, check=False, cwd=str(REPO_ROOT),
    )


COMPLETE_ROUTES = [
    "/healthz", "/readyz",
    "/api/v1/brand-signal/campaigns",
    "/api/v1/brand-signal/mentions",
]


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is required")
def test_smoke_test_passes_against_a_complete_route_table() -> None:
    server, url = _serve(COMPLETE_ROUTES)
    try:
        result = _run_smoke(url)
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "openapi.json publishes the expected frontend routes" in result.stdout


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is required")
def test_smoke_test_fails_when_a_frontend_route_disappears() -> None:
    """The check must be able to fail, or it proves nothing."""
    server, url = _serve([p for p in COMPLETE_ROUTES if "mentions" not in p])
    try:
        result = _run_smoke(url)
    finally:
        server.shutdown()
    assert result.returncode != 0
    assert "/api/v1/brand-signal/mentions" in result.stdout + result.stderr


# =============================================================================
# 4. Fail closed on an existing release directory
# =============================================================================


SHA = "a" * 40


def _release_root(tmp_path: Path) -> Path:
    root = tmp_path / "releases"
    root.mkdir()
    return root


def test_an_existing_empty_release_directory_is_rejected(tmp_path: Path) -> None:
    root = _release_root(tmp_path)
    (root / SHA).mkdir()
    result = run_snippet(f'create_release "{REPO_ROOT.as_posix()}" "{SHA}" "{root.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "unverified release directory" in result.stderr


def test_an_existing_modified_release_directory_is_rejected(tmp_path: Path) -> None:
    """A directory edited in place must never deploy as the reviewed commit."""
    root = _release_root(tmp_path)
    release = root / SHA
    release.mkdir()
    (release / "compose.yaml").write_text("services: {}\n")
    (release / "VERSION").write_text("9.9.9\n")
    result = run_snippet(f'create_release "{REPO_ROOT.as_posix()}" "{SHA}" "{root.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION
    assert "unverified release directory" in result.stderr


def test_a_correct_manifest_does_not_launder_an_altered_release(tmp_path: Path) -> None:
    """The manifest lives inside the directory whose integrity is in doubt."""
    root = _release_root(tmp_path)
    release = root / SHA
    release.mkdir()
    (release / ".release-manifest.json").write_text(
        json.dumps({"schema_version": 1, "commit": SHA, "stage": "api", "source": "git archive"})
    )
    (release / "compose.yaml").write_text("services: {tampered: {}}\n")
    result = run_snippet(f'create_release "{REPO_ROOT.as_posix()}" "{SHA}" "{root.as_posix()}"')
    assert result.returncode == EXIT_PRECONDITION, "a manifest must not be treated as proof"


def test_redeploying_the_current_release_is_refused_clearly(tmp_path: Path) -> None:
    root = _release_root(tmp_path)
    (root / SHA).mkdir()
    result = run_snippet(
        f'''
        printf '%s' "{SHA}" > "{(root / 'current.txt').as_posix()}"
        read_release_target() {{ case "$1" in *current) printf '%s' "{SHA}" ;; esac; }}
        create_release "{REPO_ROOT.as_posix()}" "{SHA}" "{root.as_posix()}"
        '''
    )
    assert result.returncode == EXIT_PRECONDITION
    assert "already the current release" in result.stderr


def test_redeploying_the_previous_release_points_at_rollback(tmp_path: Path) -> None:
    root = _release_root(tmp_path)
    (root / SHA).mkdir()
    result = run_snippet(
        f'''
        read_release_target() {{ case "$1" in *previous) printf '%s' "{SHA}" ;; esac; }}
        create_release "{REPO_ROOT.as_posix()}" "{SHA}" "{root.as_posix()}"
        '''
    )
    assert result.returncode == EXIT_PRECONDITION
    assert "rollback-compose.sh" in result.stderr


def test_write_release_manifest_never_rewrites_an_existing_one(tmp_path: Path) -> None:
    """Re-stamping a stale directory as freshly verified is the whole problem."""
    release = tmp_path / "release"
    release.mkdir()
    manifest = release / ".release-manifest.json"
    manifest.write_text('{"commit": "original"}')
    result = run_snippet(f'write_release_manifest "{release.as_posix()}" "{SHA}" api')
    assert result.returncode == EXIT_PRECONDITION
    assert "refusing to rewrite" in result.stderr
    assert json.loads(manifest.read_text())["commit"] == "original"


def test_a_fresh_release_is_still_created_from_git_archive(tmp_path: Path) -> None:
    """The gate must not have broken the normal path."""
    head = subprocess.run(  # noqa: S603 - fixed argument array
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
        check=True, cwd=str(REPO_ROOT),
    ).stdout.strip()
    root = _release_root(tmp_path)
    result = run_snippet(
        f'create_release "{REPO_ROOT.as_posix()}" "{head}" "{root.as_posix()}"'
    )
    assert result.returncode == EXIT_OK, result.stderr
    release = root / head
    assert (release / "compose.yaml").is_file()
    assert (release / "VERSION").is_file()
    assert not (release / ".git").exists(), "a release must never carry a .git directory"


# =============================================================================
# 5. The production UID/GID template
# =============================================================================


def _example_assignments() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_the_public_template_does_not_pin_a_container_identity() -> None:
    """It used to ship 10001 while documenting a 992 host -- a copied default
    built images that could not write to their own data directories."""
    values = _example_assignments()
    assert values.get("RADIO_CONTAINER_UID", "") == ""
    assert values.get("RADIO_CONTAINER_GID", "") == ""


def test_the_public_template_hardcodes_no_host_identity() -> None:
    """992 is a fact about one host and must not leak into a public template."""
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "992" not in line, f"host-specific uid leaked into the template: {line}"


def test_sourcing_the_example_leaves_the_identity_unset(tmp_path: Path) -> None:
    copied = tmp_path / "compose.env"
    copied.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    result = run_snippet(
        f'''
        set -a; source "{copied.as_posix()}"; set +a
        printf 'uid=[%s] gid=[%s]\\n' "${{RADIO_CONTAINER_UID:-}}" "${{RADIO_CONTAINER_GID:-}}"
        '''
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "uid=[] gid=[]" in result.stdout


def test_a_copied_default_resolves_to_the_host_account(tmp_path: Path) -> None:
    """With a host 'radio' account of 992:992, the deployment must land on
    992:992 and pass ownership validation -- without the operator editing
    anything."""
    copied = tmp_path / "compose.env"
    copied.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    data = tmp_path / "data"
    (data / "database").mkdir(parents=True)

    result = run_snippet(
        f'''
        set -a; source "{copied.as_posix()}"; set +a
        # Stand in for a host whose radio account is 992:992.
        id() {{ case "$1" in -u) echo 992 ;; -g) echo 992 ;; *) echo 992 ;; esac; }}
        stat() {{ case "$1" in -c) case "$2" in "%u"|"%g") echo 992 ;; "%a") echo 750 ;; esac ;; esac; }}

        HOST_IDENTITY="$(resolve_host_identity radio)"
        read -r HOST_UID HOST_GID <<<"${{HOST_IDENTITY}}"
        RADIO_CONTAINER_UID="${{RADIO_CONTAINER_UID:-${{HOST_UID}}}}"
        RADIO_CONTAINER_GID="${{RADIO_CONTAINER_GID:-${{HOST_GID}}}}"
        validate_uid_gid "${{RADIO_CONTAINER_UID}}" "${{RADIO_CONTAINER_GID}}"
        require_writable_ownership "${{RADIO_CONTAINER_UID}}" "${{RADIO_CONTAINER_GID}}" \\
            "{(data / 'database').as_posix()}"
        printf 'resolved=%s:%s\\n' "${{RADIO_CONTAINER_UID}}" "${{RADIO_CONTAINER_GID}}"
        '''
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "resolved=992:992" in result.stdout


def test_the_deployment_never_performs_a_recursive_chown() -> None:
    """Ownership is reported, never repaired. `chown` may appear only inside a
    remediation string the operator runs themselves."""
    for name in ("deploy-compose.sh", "rollback-compose.sh"):
        for line in (SCRIPTS / name).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "chown" not in stripped:
                continue
            assert "remediation" in stripped, f"{name} appears to chown directly: {stripped}"


# =============================================================================
# 6. RADIO_DEPLOY_PROFILES
# =============================================================================


def test_the_inert_deploy_profiles_variable_is_gone() -> None:
    """It read as a safety ceiling while constraining nothing: --stage is the
    single source of truth for which services start."""
    assert "RADIO_DEPLOY_PROFILES" not in _example_assignments()
    for path in (SCRIPTS / "deploy-compose.sh", SCRIPTS / "rollback-compose.sh"):
        assert "RADIO_DEPLOY_PROFILES" not in path.read_text(encoding="utf-8")
    for doc in (REPO_ROOT / "docs").glob("*.md"):
        assert "RADIO_DEPLOY_PROFILES" not in doc.read_text(encoding="utf-8"), doc.name


# =============================================================================
# 7. A selected service with no healthcheck must fail the gate
# =============================================================================


def _health_snippet(*, running: str, health: str, exitcode: str = "0", cid: str = "abc123") -> str:
    return f'''
        compose() {{ printf '{cid}\\n'; }}
        docker() {{
            case "$3" in
                *Running*)  printf '{running}\\n' ;;
                *ExitCode*) printf '{exitcode}\\n' ;;
                *Health*)   printf '{health}\\n' ;;
            esac
        }}
        wait_for_health {EXIT_HEALTH} 5 api
        echo GATE_PASSED
    '''


def test_a_service_without_a_healthcheck_cannot_pass_the_gate() -> None:
    """`none` used to count as healthy, so a service whose healthcheck was
    dropped sailed through the deployment gate unchecked."""
    result = run_snippet(_health_snippet(running="true", health="none"))
    assert result.returncode == EXIT_HEALTH
    assert "GATE_PASSED" not in result.stdout
    assert "defines no healthcheck" in result.stderr


def test_a_healthy_service_passes_the_gate() -> None:
    result = run_snippet(_health_snippet(running="true", health="healthy"))
    assert result.returncode == EXIT_OK, result.stderr
    assert "GATE_PASSED" in result.stdout


def test_a_stopped_container_fails_immediately_with_its_exit_code() -> None:
    """Waiting 300s for a container that has already exited just delays the
    same failure."""
    result = run_snippet(_health_snippet(running="false", health="none", exitcode="137"))
    assert result.returncode == EXIT_HEALTH
    assert "not running" in result.stderr and "137" in result.stderr


def test_a_missing_container_fails_the_gate() -> None:
    result = run_snippet(
        f'''
        compose() {{ printf '\\n'; }}
        wait_for_health {EXIT_HEALTH} 5 api
        echo GATE_PASSED
        '''
    )
    assert result.returncode == EXIT_HEALTH
    assert "no container" in result.stderr


def test_an_unrecognised_health_status_fails_closed() -> None:
    result = run_snippet(_health_snippet(running="true", health="weird"))
    assert result.returncode == EXIT_HEALTH
    assert "unrecognised health status" in result.stderr


def test_a_starting_container_is_waited_on_then_times_out() -> None:
    """`starting` is not a failure; the timeout is what bounds it."""
    result = run_snippet(
        _health_snippet(running="true", health="starting").replace(
            f"wait_for_health {EXIT_HEALTH} 5 api",
            f"if wait_for_health {EXIT_HEALTH} 1 api; then echo UNEXPECTED; else echo TIMED_OUT; fi",
        )
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "TIMED_OUT" in result.stdout


def test_both_scripts_use_the_shared_health_gate() -> None:
    """Two copies of this loop would drift, and one of them would keep
    treating `none` as success."""
    for name in ("deploy-compose.sh", "rollback-compose.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "wait_for_health" in text, f"{name} must use the shared gate"
        assert "healthy|none)" not in text, f"{name} still treats 'none' as healthy"


# =============================================================================
# 8. Failure recovery
# =============================================================================


def _executable_lines(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_a_failed_first_deployment_removes_what_it_started() -> None:
    """With no previous release there is nothing to roll back to, so leaving
    half-started services running would present a broken stack as deployed."""
    code = _executable_lines(SCRIPTS / "deploy-compose.sh")
    assert "down --remove-orphans" in code
    assert "first-deployment-cleaned-up" in code


def test_failure_cleanup_never_removes_volumes() -> None:
    """`down -v` is how a database disappears during an incident."""
    code = _executable_lines(SCRIPTS / "deploy-compose.sh")
    for forbidden in ("down -v", "down --volumes", "--volumes"):
        assert forbidden not in code, f"failure cleanup must not use {forbidden!r}"


def test_automatic_recovery_never_rebuilds_and_never_restores_the_database() -> None:
    """Recovery runs during an incident: a fresh build produces an artifact
    nobody reviewed, and restoring SQLite discards everything since the backup.
    """
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    start = text.index("restore_previous_release() {")
    end = text.index("write_failure_report() {")
    body = "\n".join(
        line for line in text[start:end].splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "compose \"${prev_profiles[@]}\" build" not in body
    assert " build" not in body.replace("--build-arg", ""), "recovery must not rebuild"
    assert "backup-sqlite" not in body and "restore" not in body.replace(
        "restore_previous_release", ""
    ), "recovery must not touch the database"
    # Every image the target stage needs, not just the API one: a core release
    # whose pipeline image had been pruned used to start and then fail.
    assert "require_stage_images" in body, "recovery must verify the old images still exist"


def test_recovery_does_not_reacquire_the_deployment_lock() -> None:
    """It runs inside the process that already holds the lock; calling
    rollback-compose.sh here would deadlock on it."""
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    start = text.index("restore_previous_release() {")
    end = text.index("write_failure_report() {")
    body = text[start:end]
    assert "acquire_deploy_lock" not in body
    assert "rollback-compose.sh" not in body


def test_a_failed_deployment_records_its_outcome() -> None:
    code = _executable_lines(SCRIPTS / "deploy-compose.sh")
    assert "history/failed-" in code, "a failed deployment must leave a report"
    assert '"database_restored": false' in code
    assert '"recovery": "${RECOVERY_RESULT}"' in code


def test_a_failed_deployment_never_records_success_state() -> None:
    """state.json describes what is RUNNING. A failed deploy must not claim it."""
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    start = text.index("on_failure() {")
    end = text.index("trap on_failure EXIT")
    handler = text[start:end]
    assert "point_symlink_atomic" not in handler, "a failed deploy must not move symlinks"
    assert '"smoke_test": "pass"' not in handler


def test_the_script_documents_what_actually_happens_on_failure() -> None:
    """Do not claim automatic rollback where it is not implemented."""
    header = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")[:4000]
    assert "first deployment" in header.lower()
    assert "never restores SQLite" in header or "never restore" in header.lower()
