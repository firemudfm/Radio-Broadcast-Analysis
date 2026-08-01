"""Deployment integrity: exact stages, artifact-only rollback, real backups.

The tests that matter most here drive the shell with a **fake docker binary**
placed first on PATH. It records every invocation, so these assert what the
deployment actually *does* -- that a rollback issues no `build` and no `pull`,
that an api-stage deploy never builds a pipeline image, that a stage change
removes the services it excluded -- rather than asserting that the source text
looks like it would.

Each corresponds to a defect that was present:

* every service in an active profile was built, so an api deploy built the
  pipeline image it would never start;
* `up` relied on Compose defaults, so a missing tag was silently BUILT during a
  rollback -- producing a new unreviewed image instead of restoring the old one;
* rollback took its service set from the state of the release it was rolling
  AWAY from;
* only the API image was ever checked, so a core rollback with no pipeline
  image started, then failed;
* narrowing full -> api left the workers and the LLM running;
* the recorded backup path was the pre-compression `.db` that gzip had already
  replaced.
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
EXIT_HEALTH = 72

SHA = "a1b2c3d4" * 5  # 40 hex characters
OTHER_SHA = "f9e8d7c6" * 5


def run_snippet(snippet: str, *, path_prefix: Path | None = None, **env):
    script = f'set -euo pipefail\nsource "{LIB.as_posix()}"\n{textwrap.dedent(snippet)}'
    environment = {**os.environ, **{k: str(v) for k, v in env.items()}}
    if path_prefix is not None:
        environment["PATH"] = f"{path_prefix.as_posix()}{os.pathsep}{environment['PATH']}"
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [BASH, "-c", script],
        capture_output=True, text=True, timeout=120, check=False, env=environment,
    )


# =============================================================================
# The fake docker binary
# =============================================================================


def fake_docker(
    tmp_path: Path,
    *,
    present_images: list[str] | None = None,
    health: str = "healthy",
    running: str = "true",
    ps_ids: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Write a docker stand-in that records its argv. Returns (bindir, logfile).

    `present_images` is the exact set of tags `docker image inspect` accepts, so
    a test can prove a rollback refuses to start when one image is missing.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-calls.log"
    log.write_text("", encoding="utf-8")

    present = "\n".join(present_images or [])
    ids = ps_ids if ps_ids is not None else {}
    ps_cases = "\n".join(
        f'        {name}) printf "{cid}\\n"; exit 0 ;;' for name, cid in ids.items()
    )

    script = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log.as_posix()}"

# docker image inspect <tag>
if [ "${{1:-}}" = "image" ] && [ "${{2:-}}" = "inspect" ]; then
    for candidate in "$@"; do
        while IFS= read -r known; do
            [ -n "${{known}}" ] || continue
            [ "${{candidate}}" = "${{known}}" ] && exit 0
        done <<'IMAGES'
{present}
IMAGES
    done
    exit 1
fi

# docker inspect --format <fmt> <cid>
if [ "${{1:-}}" = "inspect" ]; then
    case "${{3:-}}" in
        *Running*)  printf '{running}\\n'; exit 0 ;;
        *ExitCode*) printf '0\\n'; exit 0 ;;
        *Health*)   printf '{health}\\n'; exit 0 ;;
        *Id*)       printf 'sha256:deadbeef\\n'; exit 0 ;;
    esac
    exit 0
fi

# docker compose ... <subcommand> ...
if [ "${{1:-}}" = "compose" ]; then
    saw_ps=0
    for arg in "$@"; do
        [ "${{arg}}" = "ps" ] && saw_ps=1
    done
    if [ "${{saw_ps}}" = "1" ]; then
        service="${{@: -1}}"
        case "${{service}}" in
{ps_cases}
        esac
        exit 0
    fi
    exit 0
fi
exit 0
"""
    binary = bindir / "docker"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, log


def calls(log: Path) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def bash_path(path: Path) -> str:
    """The path as the shell sees it.

    The backup-path parser requires an absolute POSIX path, because that is
    what it receives in production (/var/lib/radio/backups/...). On Windows a
    pytest tmp_path is `C:/...`, which is absolute to Python and not to the
    shell -- so convert rather than relax the production check to accept a
    drive letter it will never legitimately see.
    """
    cygpath = shutil.which("cygpath")
    if cygpath is None:
        return path.as_posix()
    return subprocess.run(  # noqa: S603 - fixed argument array
        [cygpath, "-u", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()


# =============================================================================
# 2. The shared stage plan
# =============================================================================


APPROVED_PLAN = {
    "api": {
        "profiles": "core",
        "runtime_services": "api",
        "build_services": "api",
        "image_repos": "radio-api",
        "excluded_services": (
            "planner listener transcription-worker analysis-worker cleanup-worker llm"
        ),
    },
    "core": {
        "profiles": "core",
        "runtime_services": "api planner",
        "build_services": "api planner",
        "image_repos": "radio-api radio-pipeline",
        "excluded_services": (
            "listener transcription-worker analysis-worker cleanup-worker llm"
        ),
    },
    "full": {
        "profiles": "core pipeline llm",
        "runtime_services": (
            "api planner listener transcription-worker analysis-worker cleanup-worker llm"
        ),
        "build_services": "api planner llm",
        "image_repos": "radio-api radio-pipeline radio-llm",
        "excluded_services": "",
    },
}


@pytest.mark.parametrize("stage", sorted(APPROVED_PLAN))
def test_the_stage_plan_matches_the_approved_model(stage: str) -> None:
    for field, expected in APPROVED_PLAN[stage].items():
        result = run_snippet(f'stage_plan "{stage}" "{field}"')
        assert result.returncode == EXIT_OK, result.stderr
        assert result.stdout == expected, f"{stage}.{field}"


def test_build_services_are_one_per_image() -> None:
    """The point of a separate build set: planner represents the pipeline
    image, so api never builds a pipeline or LLM image it will not run."""
    for stage, plan in APPROVED_PLAN.items():
        builds = plan["build_services"].split()
        repos = plan["image_repos"].split()
        assert len(builds) == len(repos), f"{stage}: one build service per image"


def test_runtime_and_excluded_services_partition_the_full_set() -> None:
    """Nothing may be both selected and excluded, and nothing forgotten."""
    everything = set(APPROVED_PLAN["full"]["runtime_services"].split())
    for stage, plan in APPROVED_PLAN.items():
        selected = set(plan["runtime_services"].split())
        excluded = set(plan["excluded_services"].split())
        assert not (selected & excluded), f"{stage}: a service is both run and excluded"
        assert selected | excluded == everything, f"{stage}: a service is unaccounted for"


def test_an_unknown_stage_is_refused() -> None:
    assert run_snippet('stage_plan "everything" profiles').returncode == EXIT_USAGE


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("api", f"radio-api:{SHA}"),
        ("core", f"radio-api:{SHA} radio-pipeline:{SHA}"),
        ("full", f"radio-api:{SHA} radio-pipeline:{SHA} radio-llm:{SHA}"),
    ],
)
def test_required_images_are_sha_tagged(stage: str, expected: str) -> None:
    result = run_snippet(f'stage_required_images "{stage}" "{SHA}"')
    assert result.stdout.split() == expected.split()
    assert "latest" not in result.stdout


# =============================================================================
# 3 + 4. Stage-restricted build, required images, artifact-only start
# =============================================================================


def test_required_image_validation_reports_every_missing_image(tmp_path: Path) -> None:
    """One round-trip per missing image during an incident is the wrong shape
    of feedback."""
    bindir, _ = fake_docker(tmp_path, present_images=[f"radio-api:{SHA}"])
    result = run_snippet(f'require_stage_images full "{SHA}"', path_prefix=bindir)
    assert result.returncode != 0
    assert f"radio-pipeline:{SHA}" in result.stderr
    assert f"radio-llm:{SHA}" in result.stderr


def test_api_stage_does_not_require_pipeline_or_llm_images(tmp_path: Path) -> None:
    bindir, _ = fake_docker(tmp_path, present_images=[f"radio-api:{SHA}"])
    result = run_snippet(f'require_stage_images api "{SHA}"', path_prefix=bindir)
    assert result.returncode == EXIT_OK, result.stderr


def test_core_stage_requires_the_pipeline_image(tmp_path: Path) -> None:
    bindir, _ = fake_docker(tmp_path, present_images=[f"radio-api:{SHA}"])
    result = run_snippet(f'require_stage_images core "{SHA}"', path_prefix=bindir)
    assert result.returncode != 0
    assert f"radio-pipeline:{SHA}" in result.stderr


def test_full_stage_requires_the_llm_image(tmp_path: Path) -> None:
    bindir, _ = fake_docker(
        tmp_path, present_images=[f"radio-api:{SHA}", f"radio-pipeline:{SHA}"]
    )
    result = run_snippet(f'require_stage_images full "{SHA}"', path_prefix=bindir)
    assert result.returncode != 0
    assert f"radio-llm:{SHA}" in result.stderr


def test_unused_image_fields_are_null_not_fabricated(tmp_path: Path) -> None:
    """An api deployment must not record an LLM image as if it were verified."""
    bindir, _ = fake_docker(tmp_path, present_images=[f"radio-api:{SHA}"])
    result = run_snippet(
        f'''
        printf '{{"llm": %s, "api": %s}}' \\
            "$(json_image_field api "{SHA}" radio-llm tag)" \\
            "$(json_image_field api "{SHA}" radio-api tag)"
        ''',
        path_prefix=bindir,
    )
    assert result.returncode == EXIT_OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["llm"] is None
    assert payload["api"] == f"radio-api:{SHA}"


def _script_body(path: Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    body = text[text.index(start):text.index(end)]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def test_deploy_builds_only_the_stage_build_services() -> None:
    code = _script_body(
        SCRIPTS / "deploy-compose.sh", "14/16 Building images", "15/16 Backing up"
    )
    # Narrower still than the stage's build set: only the images that are
    # actually missing, so promoting api X -> core X reuses radio-api:X.
    assert 'build "${MISSING_BUILDS[@]}"' in code, "build must be restricted to missing images"
    assert "missing_stage_build_services" in code
    assert 'compose "${PROFILES[@]}" build 2>&1' not in code, "unrestricted build returned"


@pytest.mark.parametrize(
    ("script", "start", "end"),
    [
        ("deploy-compose.sh", "16/16 Starting services", "Recording deployment state"),
        ("rollback-compose.sh", "11/11 Starting the target release", "Symlinks and state"),
    ],
)
def test_every_start_is_artifact_only(script: str, start: str, end: str) -> None:
    code = _script_body(SCRIPTS / script, start, end)
    assert "--no-build" in code, f"{script} must not let Compose build at start"
    assert "--pull never" in code, f"{script} must not let Compose pull at start"


def test_recovery_is_artifact_only() -> None:
    code = _script_body(
        SCRIPTS / "deploy-compose.sh", "restore_previous_release() {", "write_failure_report"
    )
    assert "--no-build" in code and "--pull never" in code
    assert "require_stage_images" in code, "recovery must verify images before touching containers"


@pytest.mark.parametrize("script", ["rollback-compose.sh", "deploy-compose.sh"])
def test_no_script_deletes_volumes(script: str) -> None:
    code = "\n".join(
        line for line in (SCRIPTS / script).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("down -v", "--volumes", "rm -v", "rm --volumes"):
        assert forbidden not in code, f"{script} must never delete volumes ({forbidden})"


# =============================================================================
# 5. Release manifest validation
# =============================================================================


def make_release(
    root: Path,
    sha: str,
    *,
    stage: str = "api",
    schema: int | str = 1,
    commit: str | None = None,
    source: str | None = "git archive",
    manifest: bool = True,
    raw: str | None = None,
) -> Path:
    # Release identity is commit + stage: <root>/<sha>/<stage>.
    release = root / sha / stage
    (release / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("VERSION", "compose.yaml", "compose.prod.yaml"):
        (release / name).write_text("x\n", encoding="utf-8")
    (release / "scripts" / "smoke-test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    if manifest:
        document: dict = {"schema_version": schema, "commit": commit or sha, "stage": stage}
        if source is not None:
            document["source"] = source
        (release / ".release-manifest.json").write_text(
            raw if raw is not None else json.dumps(document), encoding="utf-8"
        )
    return release


def validate(release: Path, expected: str, stage: str = "api"):
    return run_snippet(
        f'validate_release_manifest "{release.as_posix()}" "{expected}" "{stage}"'
    )


@pytest.mark.parametrize("stage", ["api", "core", "full"])
def test_a_valid_manifest_yields_its_stage(tmp_path: Path, stage: str) -> None:
    release = make_release(tmp_path, SHA, stage=stage)
    result = validate(release, SHA, stage)
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout == stage


def test_a_missing_manifest_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path, SHA, manifest=False)
    result = validate(release, SHA)
    assert result.returncode != 0
    assert "no .release-manifest.json" in result.stderr


def test_malformed_manifest_json_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path, SHA, raw="{not json")
    assert "not valid JSON" in validate(release, SHA).stderr


def test_a_commit_mismatch_is_rejected(tmp_path: Path) -> None:
    """The manifest must describe the release actually being started."""
    release = make_release(tmp_path, SHA, commit=OTHER_SHA)
    assert "does not match the requested target" in validate(release, SHA).stderr


def test_a_directory_name_mismatch_is_rejected(tmp_path: Path) -> None:
    """A correct-looking release tree moved under the wrong commit directory."""
    release = make_release(tmp_path, OTHER_SHA, commit=OTHER_SHA)
    other_parent = tmp_path / SHA
    other_parent.mkdir(parents=True, exist_ok=True)
    release.rename(other_parent / "api")
    result = validate(other_parent / "api", OTHER_SHA, "api")
    assert result.returncode != 0
    assert "does not match the requested commit" in result.stderr


def test_an_invalid_stage_is_a_hard_failure(tmp_path: Path) -> None:
    """Silently defaulting to api would start the wrong service set."""
    release = make_release(tmp_path, SHA, stage="everything")
    for requested in ("api", "core", "full"):
        result = validate(release, SHA, requested)
        assert result.returncode != 0, requested
        assert result.stdout.strip() == "", "an invalid stage must yield nothing"


def test_a_short_or_uppercase_commit_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path, SHA, commit=SHA.upper())
    assert "not a full lower-case" in validate(release, SHA).stderr


def test_an_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path, SHA, schema=2)
    assert "unsupported manifest schema_version" in validate(release, SHA).stderr


def test_a_foreign_source_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path, SHA, source="rsync from a laptop")
    assert "not exactly" in validate(release, SHA).stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_a_symlinked_manifest_is_rejected(tmp_path: Path) -> None:
    """Otherwise the validated bytes and the deployed bytes can differ."""
    release = make_release(tmp_path, SHA, manifest=False)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"schema_version": 1, "commit": SHA, "stage": "full"}))
    (release / ".release-manifest.json").symlink_to(real)
    result = validate(release, SHA)
    assert result.returncode != 0
    assert "symlink" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_a_symlinked_release_directory_is_rejected(tmp_path: Path) -> None:
    real = make_release(tmp_path / "real", SHA)  # .../real/<sha>/api
    link_commit_dir = tmp_path / "links" / SHA
    link_commit_dir.mkdir(parents=True)
    (link_commit_dir / "api").symlink_to(real, target_is_directory=True)
    result = validate(link_commit_dir / "api", SHA, "api")
    assert result.returncode != 0
    assert "symlink" in result.stderr


def test_a_missing_required_release_file_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path, SHA)
    (release / "compose.prod.yaml").unlink()
    assert "missing regular file" in validate(release, SHA).stderr


@pytest.mark.parametrize(
    ("current_stage", "target_stage"),
    [("full", "api"), ("api", "full"), ("full", "core"), ("core", "api")],
)
def test_rollback_takes_its_stage_from_the_target_not_the_current_state(
    tmp_path: Path, current_stage: str, target_stage: str
) -> None:
    """Rolling a full deployment back to an api release must start api services.

    Reading the stage from state.json describes the release being rolled AWAY
    from, so it would start workers against code that never shipped with them.
    """
    release = make_release(tmp_path, SHA, stage=target_stage)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "current_commit": OTHER_SHA,
        "current_stage": current_stage,
        "stage": current_stage,
    }))
    result = run_snippet(
        f'''
        resolved="$(validate_release_manifest "{release.as_posix()}" "{SHA}" "{target_stage}")"
        current="$(read_state_field "{state.as_posix()}" current_stage)"
        printf 'target=%s current=%s services=%s\\n' \\
            "${{resolved}}" "${{current}}" "$(stage_plan "${{resolved}}" runtime_services)"
        '''
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert f"target={target_stage}" in result.stdout
    assert f"current={current_stage}" in result.stdout
    assert APPROVED_PLAN[target_stage]["runtime_services"] in result.stdout
    # The service set is the TARGET's, never the one still recorded as current.
    if current_stage != target_stage:
        assert result.stdout.split("services=")[1].strip() != \
            APPROVED_PLAN[current_stage]["runtime_services"]


def test_rollback_does_not_read_its_stage_from_deployment_state() -> None:
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    body = text[text.index("Validating the target release manifest"):text.index("Validating target images")]
    assert "validate_release_manifest" in body
    assert 'STAGE_NAME="$(read_state_field' not in body


# =============================================================================
# 6. Exact service reconciliation
# =============================================================================


RECONCILE_CASES = [
    ("api", ["planner", "listener", "transcription-worker",
             "analysis-worker", "cleanup-worker", "llm"]),
    ("core", ["listener", "transcription-worker",
              "analysis-worker", "cleanup-worker", "llm"]),
]


@pytest.mark.parametrize(("stage", "expected_removed"), RECONCILE_CASES)
def test_reconciliation_removes_every_excluded_service(
    tmp_path: Path, stage: str, expected_removed: list[str]
) -> None:
    bindir, log = fake_docker(tmp_path)
    result = run_snippet(
        f'''
        compose() {{ docker compose "$@"; }}
        reconcile_stage_services "{stage}"
        ''',
        path_prefix=bindir,
    )
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    recorded = "\n".join(calls(log))
    assert "compose" in recorded
    stopped = [line for line in calls(log) if " stop " in f" {line} "]
    removed = [line for line in calls(log) if " rm " in f" {line} "]
    assert stopped and removed, "excluded services must be stopped AND removed"
    for service in expected_removed:
        assert service in stopped[0], f"{service} must be stopped at stage {stage}"
        assert service in removed[0], f"{service} must be removed at stage {stage}"


def test_reconciliation_never_passes_a_volume_flag(tmp_path: Path) -> None:
    """`rm -v` during a stage change is how bind-mounted evidence disappears."""
    bindir, log = fake_docker(tmp_path)
    run_snippet(
        'compose() { docker compose "$@"; }\nreconcile_stage_services api',
        path_prefix=bindir,
    )
    for line in calls(log):
        assert " -v" not in f" {line}", f"volume flag in: {line}"
        assert "--volumes" not in line, f"volume flag in: {line}"


def test_reconciliation_enables_every_profile(tmp_path: Path) -> None:
    """A service in an inactive profile is invisible to Compose -- and an
    invisible running container is exactly the problem being fixed."""
    bindir, log = fake_docker(tmp_path)
    run_snippet(
        'compose() { docker compose "$@"; }\nreconcile_stage_services api',
        path_prefix=bindir,
    )
    stop_calls = [line for line in calls(log) if " stop " in f" {line} "]
    assert stop_calls
    for profile in ("core", "pipeline", "llm"):
        assert f"--profile {profile}" in stop_calls[0]


def test_full_stage_excludes_nothing(tmp_path: Path) -> None:
    bindir, log = fake_docker(tmp_path)
    result = run_snippet(
        'compose() { docker compose "$@"; }\nreconcile_stage_services full',
        path_prefix=bindir,
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert calls(log) == [], "nothing to reconcile means nothing to run"


def test_reconciliation_fails_when_an_excluded_service_survives(tmp_path: Path) -> None:
    """The verification step must be able to fail, or it proves nothing."""
    bindir, log = fake_docker(
        tmp_path, running="true", ps_ids={"llm": "llm-container"}
    )
    result = run_snippet(
        'compose() { docker compose "$@"; }\nreconcile_stage_services api',
        path_prefix=bindir,
    )
    assert result.returncode != 0
    assert "still running" in result.stderr
    assert "llm" in result.stderr


@pytest.mark.parametrize("script", ["deploy-compose.sh", "rollback-compose.sh"])
def test_reconciliation_runs_before_success_is_recorded(script: str) -> None:
    full = (SCRIPTS / script).read_text(encoding="utf-8")
    # Skip the recovery helper, which legitimately has its own earlier copy of
    # this sequence; assert the ordering of the main deployment path.
    marker = "Starting services and verifying health" if "deploy" in script         else "Starting the target release"
    text = full[full.index(marker):]
    reconcile = text.index("reconcile_stage_services \"${STAGE")
    smoke = text.index("scripts/smoke-test.sh")
    symlink = text.index("point_symlink_atomic")
    assert reconcile < smoke < symlink, (
        f"{script}: reconcile, then smoke, then move symlinks"
    )


# =============================================================================
# 7. Stage-aware readiness
# =============================================================================


def test_the_container_healthcheck_probes_liveness_not_pipeline_readiness() -> None:
    """/readyz needs worker heartbeats in shared_sqs, so an api-stage rollout
    on a shared_sqs host could never report healthy."""
    source = (REPO_ROOT / "docker" / "healthchecks" / "api.py").read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert '/healthz' in executable
    assert 'url = f"http://127.0.0.1:{port}/readyz"' not in executable


def test_the_container_healthcheck_still_requires_the_database() -> None:
    source = (REPO_ROOT / "docker" / "healthchecks" / "api.py").read_text(encoding="utf-8")
    assert 'database != "ok"' in source, "a container that cannot reach SQLite is not healthy"


def test_readiness_semantics_are_unchanged_for_a_complete_stack() -> None:
    """The cycle was fixed by changing the startup edge, not by weakening
    /readyz."""
    source = (REPO_ROOT / "app" / "services" / "pipeline_status.py").read_text(encoding="utf-8")
    assert 'all(checks[role] == "ok" for role in REQUIRED_ROLES)' in source


@pytest.mark.parametrize("stage", ["api", "core", "full"])
def test_the_smoke_test_accepts_every_stage(stage: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, str(SCRIPTS / "smoke-test.sh"), "--stage", stage, "http://127.0.0.1:9"],
        capture_output=True, text=True, timeout=120, check=False, cwd=str(REPO_ROOT),
    )
    # Nothing is listening on port 9, so it must fail -- but on the API being
    # unreachable, not on argument parsing.
    assert result.returncode != EXIT_USAGE
    assert f"stage {stage}" in result.stdout


def test_the_smoke_test_refuses_an_unknown_stage() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, str(SCRIPTS / "smoke-test.sh"), "--stage", "everything"],
        capture_output=True, text=True, timeout=60, check=False, cwd=str(REPO_ROOT),
    )
    assert result.returncode == EXIT_USAGE


def test_only_the_full_stage_demands_every_worker_role() -> None:
    smoke = (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8")
    assert 'core) REQUIRED_COMPONENTS="planner"' in smoke
    assert "full) REQUIRED_COMPONENTS=\"listener transcription_worker analysis_worker planner\"" in smoke
    assert 'if [ "${STAGE}" = "full" ]; then' in smoke, "readyz=true only at full"


def test_the_core_stage_reports_partial_pipeline_readiness() -> None:
    smoke = (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8")
    assert "PARTIAL" in smoke


def test_the_api_stage_does_not_claim_the_pipeline_is_ready() -> None:
    smoke = (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8")
    assert "NOT claimed to be ready" in smoke


def test_only_the_full_stage_requires_configured_queues() -> None:
    smoke = (SCRIPTS / "smoke-test.sh").read_text(encoding="utf-8")
    assert "queues_configured" in smoke


@pytest.mark.parametrize("script", ["deploy-compose.sh", "rollback-compose.sh"])
def test_every_smoke_invocation_passes_the_stage(script: str) -> None:
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    invocations = [line for line in text.splitlines() if "smoke-test.sh" in line and "bash " in line]
    assert invocations, f"{script} must run the smoke test"
    for line in invocations:
        assert "--stage" in line, f"{script}: smoke invoked without a stage: {line}"


# =============================================================================
# 8. The backup path contract
# =============================================================================


def test_the_backup_emits_exactly_one_machine_readable_line() -> None:
    source = (SCRIPTS / "backup-sqlite.sh").read_text(encoding="utf-8")
    emitters = [line for line in source.splitlines() if line.startswith('echo "BACKUP_PATH=')]
    assert len(emitters) == 1, "exactly one BACKUP_PATH= emitter"
    assert source.rstrip().endswith('echo "BACKUP_PATH=${TARGET}"'), (
        "the marker must be emitted last, after compression and chmod"
    )


def test_the_parser_accepts_a_single_valid_line(tmp_path: Path) -> None:
    backup = tmp_path / "radio-20260101T000000Z.db.gz"
    backup.write_bytes(b"gz")
    output = tmp_path / "out.txt"
    output.write_text(
        f"==> Backing up\n    {tmp_path / 'radio.db'} (10 bytes) integrity ok\n"
        f"backup-sqlite: PASS\nBACKUP_PATH={bash_path(backup)}\n",
        encoding="utf-8",
    )
    result = run_snippet(f'parse_backup_path "{output.as_posix()}"')
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout == bash_path(backup)


def test_the_parser_never_returns_the_stale_uncompressed_path(tmp_path: Path) -> None:
    """The old parser read the first path in the human output -- the `.db` that
    gzip had already replaced, so state recorded a file that did not exist."""
    stale = tmp_path / "radio-20260101T000000Z.db"
    final = tmp_path / "radio-20260101T000000Z.db.gz"
    final.write_bytes(b"gz")
    output = tmp_path / "out.txt"
    output.write_text(
        f"    {stale.as_posix()} (10 bytes) integrity ok\n"
        f"    compressed to {final.as_posix()}\n"
        f"BACKUP_PATH={bash_path(final)}\n",
        encoding="utf-8",
    )
    result = run_snippet(f'parse_backup_path "{output.as_posix()}"')
    assert result.stdout == bash_path(final)
    assert not stale.exists()


def test_the_parser_rejects_missing_and_duplicate_markers(tmp_path: Path) -> None:
    backup = tmp_path / "b.db.gz"
    backup.write_bytes(b"gz")
    none_file = tmp_path / "none.txt"
    none_file.write_text("backup-sqlite: PASS\n", encoding="utf-8")
    assert run_snippet(f'parse_backup_path "{none_file.as_posix()}"').returncode != 0

    many = tmp_path / "many.txt"
    many.write_text(
        f"BACKUP_PATH={backup.as_posix()}\nBACKUP_PATH={backup.as_posix()}\n",
        encoding="utf-8",
    )
    result = run_snippet(f'parse_backup_path "{many.as_posix()}"')
    assert result.returncode != 0
    assert "found 2" in result.stderr


def test_the_parser_rejects_a_relative_path(tmp_path: Path) -> None:
    output = tmp_path / "out.txt"
    output.write_text("BACKUP_PATH=backups/radio.db.gz\n", encoding="utf-8")
    assert "not absolute" in run_snippet(f'parse_backup_path "{output.as_posix()}"').stderr


def test_the_parser_rejects_a_path_that_does_not_exist(tmp_path: Path) -> None:
    output = tmp_path / "out.txt"
    output.write_text(f"BACKUP_PATH={bash_path(tmp_path)}/gone.db.gz\n", encoding="utf-8")
    assert "does not exist" in run_snippet(f'parse_backup_path "{output.as_posix()}"').stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are not available")
def test_the_parser_rejects_a_symlinked_backup(tmp_path: Path) -> None:
    real = tmp_path / "real.db.gz"
    real.write_bytes(b"gz")
    link = tmp_path / "link.db.gz"
    link.symlink_to(real)
    output = tmp_path / "out.txt"
    output.write_text(f"BACKUP_PATH={link.as_posix()}\n", encoding="utf-8")
    assert "symlink" in run_snippet(f'parse_backup_path "{output.as_posix()}"').stderr


def test_the_backup_verifies_containment_and_mode() -> None:
    source = (SCRIPTS / "backup-sqlite.sh").read_text(encoding="utf-8")
    assert "escapes the backup root" in source
    assert "broader than 0600" in source


@pytest.mark.parametrize("script", ["deploy-compose.sh", "rollback-compose.sh"])
def test_no_script_truncates_the_backup_producer(script: str) -> None:
    """`awk '{print; exit}'` closed the pipe and SIGPIPE'd the backup script
    while it was still pruning and uploading."""
    code = "\n".join(
        line for line in (SCRIPTS / script).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "exit}" not in code, f"{script} must not terminate the backup pipeline early"
    assert "parse_backup_path" in code, f"{script} must use the shared parser"


@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("deploy-compose.sh", '"backup_path": "${BACKUP_PATH}"'),
        ("rollback-compose.sh", '"backup_path": "${BACKUP_PATH}"'),
    ],
)
def test_state_records_the_parsed_backup_path(script: str, marker: str) -> None:
    assert marker in (SCRIPTS / script).read_text(encoding="utf-8")


# =============================================================================
# 9. Rollback enforces the same host gates as deploy
# =============================================================================


ROLLBACK_GATES = [
    ("resolve_host_identity", "must resolve the host radio account"),
    ("validate_uid_gid", "must validate the runtime identity"),
    ("require_writable_ownership", "must validate directory ownership"),
    ("require_env_file", "must check env-file presence and permissions"),
    ("reject_static_aws_credentials", "must reject static AWS credentials"),
    ("reject_placeholder_secret", "must reject a placeholder secret"),
    ("validate_publish_host", "must require acknowledgement for direct HTTP"),
    ("require_mountpoint", "must refuse a non-mount-point data root"),
    ("require_free_space", "must check disk space before taking a backup"),
]


@pytest.mark.parametrize(("gate", "why"), ROLLBACK_GATES)
def test_rollback_cannot_bypass_a_deploy_gate(gate: str, why: str) -> None:
    """Rollback changes the host as much as a deploy, and is the path taken
    under time pressure. It must not be the documented way around the gates."""
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    assert gate in text, f"rollback {why}"


def test_rollback_treats_a_blank_uid_as_auto_detect() -> None:
    """Defaulting blank to 10001 on a 992 host produces containers that cannot
    write their own bind mounts."""
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    assert 'RADIO_CONTAINER_UID:-${HOST_UID}' in text
    assert 'export RADIO_CONTAINER_UID="${RADIO_CONTAINER_UID:-10001}"' not in text


def test_rollback_does_not_print_environment_file_contents() -> None:
    text = (SCRIPTS / "rollback-compose.sh").read_text(encoding="utf-8")
    assert "contents not printed" in text
    assert "cat \"${ENV_DIR}" not in text


# =============================================================================
# 10. Accurate persistent-state reporting on failure
# =============================================================================


FAILURE_FIELDS = [
    "attempted_commit", "stage", "exit_code", "failure_phase", "containers_touched",
    "backup_created", "backup_path", "migration_started", "migration_completed",
    "database_restored", "release_path", "deploy_log", "recovery",
]


@pytest.mark.parametrize("field", FAILURE_FIELDS)
def test_the_failure_report_records_every_required_field(field: str) -> None:
    code = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    assert f'"{field}":' in code


PHASES = ["validation", "build", "backup", "migration", "start", "health", "reconcile", "smoke"]


@pytest.mark.parametrize("phase", PHASES)
def test_every_failure_phase_is_labelled(phase: str) -> None:
    """`failed` is not an answer. Which phase decides whether the database was
    touched, and therefore what the operator does next."""
    code = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    assert f'FAILURE_PHASE="{phase}"' in code


def test_migration_progress_is_tracked_separately_from_completion() -> None:
    """A migration that started and did not finish may still have applied
    earlier migrations in the run."""
    code = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    assert "MIGRATION_STARTED=1" in code
    assert "MIGRATION_COMPLETED=1" in code
    assert code.index("MIGRATION_STARTED=1") < code.index("MIGRATION_COMPLETED=1")


def test_a_failure_before_containers_change_still_writes_a_report() -> None:
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    handler = text[text.index("on_failure() {"):text.index("trap on_failure EXIT")]
    untouched = handler[handler.index('CONTAINERS_TOUCHED}" -eq 0'):]
    assert "write_failure_report" in untouched.split("fail \"deployment failed in phase '${FAILURE_PHASE}', AFTER")[0]


def test_a_failure_before_containers_change_does_not_claim_nothing_happened() -> None:
    """A backup may exist and migrations may already be applied."""
    text = (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8")
    assert "before any RUNNING CONTAINER was changed" in text
    assert "report_persistent_state" in text


def test_the_database_is_never_restored_automatically() -> None:
    code = "\n".join(
        line for line in (SCRIPTS / "deploy-compose.sh").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert '"database_restored": false' in code
    assert "restore-sqlite" not in code and "sqlite3 .restore" not in code
