"""There is exactly one production processing pipeline.

The old design carried two runtimes behind `RADIO_PIPELINE_MODE`, defaulting to
the one nobody intended to deploy. These tests exist so a second pipeline cannot
grow back — not as a mode, not as a systemd unit, not as an S3 poll standing in
for a queue, and not as a process per campaign.

The distinction that matters throughout: a **test double** (memory queue, fake
ASR) is not a second pipeline, and a **deployment stage** (api/core/full) is not
a second pipeline. Both are kept; both are asserted to stay kept.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
APP = REPO_ROOT / "app"
COMPOSE = REPO_ROOT / "compose.yaml"

BASE = {"RADIO_S3_BUCKET": "bucket", "RADIO_AUDIO_TOKEN_SECRET": "x" * 48}


def app_sources() -> list[Path]:
    return [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]


def executable_lines(path: Path) -> str:
    """Source without comment lines and without docstring prose.

    Comments and docstrings legitimately NAME the things they forbid ("not one
    process per campaign"), so a raw scan matches the explanation rather than a
    real code path.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# =============================================================================
# A. No pipeline mode exists
# =============================================================================


def test_the_settings_model_has_no_pipeline_mode() -> None:
    assert "RADIO_PIPELINE_MODE" not in Settings.model_fields


def test_no_module_branches_on_a_pipeline_mode() -> None:
    """A single `if mode == ...` is how the second runtime comes back."""
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in app_sources()
        if "shared_pipeline_enabled" in executable_lines(path)
    ]
    assert not offenders, f"pipeline-mode branch survives in {offenders}"


def test_only_the_startup_guard_names_the_removed_setting() -> None:
    """It has to name it to reject it; nothing else may read it."""
    naming = {
        path.relative_to(REPO_ROOT)
        for path in app_sources()
        if "RADIO_PIPELINE_MODE" in executable_lines(path)
    }
    assert naming <= {Path("app/config.py")}, f"unexpected readers: {naming}"


def test_a_stale_legacy_environment_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RADIO_PIPELINE_MODE", "legacy")
    with pytest.raises(ValueError) as error:
        Settings(**BASE)
    message = str(error.value)
    assert "no longer exist" in message
    assert "RADIO_PIPELINE_MODE" in message
    assert "ADR-single-shared-sqs-pipeline" in message, "the message must say where to look"


@pytest.mark.parametrize(
    "name", ["RADIO_PIPELINE_MODE", "RADIO_MAX_ACTIVE_STATIONS", "RADIO_RECONCILER_POLL_SECONDS"]
)
def test_every_removed_setting_is_rejected_by_name(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`extra="ignore"` would drop these silently, and the operator would never
    learn their file was stale."""
    monkeypatch.setenv(name, "1")
    with pytest.raises(ValueError, match=name):
        Settings(**BASE)


# =============================================================================
# B. No legacy runtime survives
# =============================================================================


def test_the_systemd_station_reconciler_is_gone() -> None:
    assert not (APP / "station_reconciler.py").exists()


def test_no_module_manages_systemd_units() -> None:
    """The API never touched systemd; the reconciler did, and it is gone."""
    for path in app_sources():
        code = executable_lines(path)
        for forbidden in ("systemctl", "journalctl", "radio-capture@", "radio-uploader@"):
            assert forbidden not in code, f"{path.name} still drives systemd ({forbidden})"


def test_no_systemd_unit_template_remains() -> None:
    units = list((REPO_ROOT / "deploy").rglob("*.service"))
    assert not units, f"legacy unit templates remain: {units}"


def test_no_production_path_polls_s3_as_a_queue() -> None:
    """S3 is durable storage. Using it as a queue was the legacy design and is
    how ordering per station was lost."""
    for path in app_sources():
        code = executable_lines(path)
        if "list_objects_v2" not in code:
            continue
        # Legitimate uses: the station catalogue and health probes. A worker
        # polling for work is not.
        assert "worker" not in str(path), f"{path} polls S3 for work"


def test_no_process_is_created_per_campaign_or_keyword() -> None:
    for path in app_sources():
        code = executable_lines(path)
        for forbidden in ("Popen", "fork(", "multiprocessing.Process"):
            if forbidden not in code:
                continue
            # The listener spawns one ffmpeg per STATION. That is the design.
            assert "listener" in str(path) or "preview" in str(path), (
                f"{path} spawns a process outside the per-station listener"
            )


# =============================================================================
# C. Test doubles are kept, and barred from production
# =============================================================================


def test_the_memory_queue_backend_still_exists() -> None:
    """Deterministic tests need it. It is a double, not a pipeline."""
    settings = Settings(**BASE, RADIO_QUEUE_BACKEND="memory")
    assert settings.RADIO_QUEUE_BACKEND == "memory"


def test_the_fake_asr_backend_still_exists() -> None:
    settings = Settings(**BASE, RADIO_ASR_BACKEND="fake")
    assert settings.RADIO_ASR_BACKEND == "fake"


def test_the_local_segment_store_still_exists() -> None:
    assert (APP / "pipeline" / "local_segment_store.py").exists()


def test_the_s3_segment_store_adapter_is_retained() -> None:
    """A storage backend, not a second pipeline."""
    assert (APP / "pipeline" / "s3_segment_store.py").exists()


def test_production_refuses_the_memory_queue() -> None:
    with pytest.raises(ValueError, match="RADIO_QUEUE_BACKEND=sqs"):
        Settings(**BASE, APP_ENV="production", RADIO_QUEUE_BACKEND="memory")


def test_production_refuses_fake_asr() -> None:
    with pytest.raises(ValueError, match="real ASR backend"):
        Settings(
            **BASE,
            APP_ENV="production",
            RADIO_QUEUE_BACKEND="sqs",
            RADIO_TRANSCRIPTION_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/t.fifo",
            RADIO_ANALYSIS_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/a.fifo",
            RADIO_ASR_BACKEND="fake",
        )


def test_production_requires_fifo_queue_urls() -> None:
    """Per-station ordering is correctness, not preference."""
    with pytest.raises(ValueError, match="FIFO"):
        Settings(
            **BASE,
            RADIO_QUEUE_BACKEND="sqs",
            RADIO_TRANSCRIPTION_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/t",
            RADIO_ANALYSIS_QUEUE_URL="https://sqs.eu-north-1.amazonaws.com/1/a.fifo",
        )


# =============================================================================
# D. Deployment stages are kept -- they are not pipelines
# =============================================================================


@pytest.mark.parametrize("stage", ["api", "core", "full"])
def test_the_deployment_stage_still_exists(stage: str) -> None:
    """Rollout safety, not a processing alternative."""
    lib = (REPO_ROOT / "scripts" / "lib" / "deploy-common.sh").read_text(encoding="utf-8")
    assert f"{stage}:profiles" in lib
    assert f"{stage}:runtime_services" in lib


def test_the_full_stage_runs_every_service_of_the_one_pipeline() -> None:
    lib = (REPO_ROOT / "scripts" / "lib" / "deploy-common.sh").read_text(encoding="utf-8")
    line = next(row for row in lib.splitlines() if "full:runtime_services" in row)
    for service in (
        "api", "planner", "listener", "transcription-worker",
        "analysis-worker", "cleanup-worker", "llm",
    ):
        assert service in line, f"the full stage must run {service}"


# =============================================================================
# E. Compose: exactly seven application services
# =============================================================================


EXPECTED_SERVICES = [
    "analysis-worker", "api", "cleanup-worker", "listener",
    "llm", "planner", "transcription-worker",
]


def compose_services() -> list[str]:
    text = COMPOSE.read_text(encoding="utf-8")
    body = text[text.index("\nservices:"):]
    return sorted(re.findall(r"^  ([a-z][a-z0-9-]*):$", body, re.MULTILINE))


def test_production_compose_has_exactly_seven_services() -> None:
    assert compose_services() == EXPECTED_SERVICES
    assert len(EXPECTED_SERVICES) == 7


def test_only_the_api_publishes_a_port() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("\n    ports:") == 1, "exactly one service may publish"


def test_the_llm_port_is_never_published() -> None:
    """8790 is an unauthenticated inference endpoint."""
    text = COMPOSE.read_text(encoding="utf-8")
    body = text[text.index("\n  llm:"):]
    # Comment-stripped: the service comment deliberately says "NO `ports:`".
    executable = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ports:" not in executable, "the LLM must stay internal"
    assert "expose:" in executable


def test_no_container_mounts_the_docker_socket() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in text, "the Docker socket is root on the host"


# =============================================================================
# F. Capacity truth
# =============================================================================


def test_the_default_active_capacity_is_one() -> None:
    assert Settings(**BASE).RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 1


def test_requested_capacity_is_separate_and_larger() -> None:
    settings = Settings(**BASE)
    assert settings.RADIO_MAX_REQUESTED_UNIQUE_STATIONS == 1000
    assert settings.RADIO_MAX_ACTIVE_UNIQUE_STATIONS == 1


def test_no_document_claims_a_thousand_live_streams() -> None:
    """The control plane scales to 1,000. Decoding does not, and nothing may
    imply it does."""
    claims = [
        "1,000 simultaneous live",
        "1000 simultaneous live",
        "1,000 concurrent streams",
        "supports 1,000 live",
    ]
    for document in list(REPO_ROOT.glob("*.md")) + list((REPO_ROOT / "docs").rglob("*.md")):
        text = document.read_text(encoding="utf-8")
        for claim in claims:
            index = text.find(claim)
            if index == -1:
                continue
            window = text[max(0, index - 120):index + 120].lower()
            assert "not" in window or "does not" in window, (
                f"{document.name} appears to claim {claim!r}"
            )


def test_the_capacity_document_states_what_is_unmeasured() -> None:
    text = (REPO_ROOT / "docs" / "CAPACITY.md").read_text(encoding="utf-8")
    assert "No benchmark has been run" in text
    assert "does not support 1,000 simultaneous live streams" in (
        (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    )


def test_the_benchmark_harness_exists_and_changes_nothing() -> None:
    script = REPO_ROOT / "scripts" / "benchmark-capacity.sh"
    assert script.exists()
    code = executable_lines(script)
    assert "changes no configuration" in script.read_text(encoding="utf-8")
    for forbidden in ("sed -i", "docker compose up", "systemctl start"):
        assert forbidden not in code, f"a benchmark must not {forbidden}"


# =============================================================================
# G. No SSH, no legacy deployment
# =============================================================================


def test_no_ssh_deployment_reference_remains() -> None:
    for pattern in ("EC2_SSH_KEY", "EC2_HOST"):
        for path in (REPO_ROOT / ".github").rglob("*.yml"):
            assert pattern not in path.read_text(encoding="utf-8"), path.name
        for path in (REPO_ROOT / "scripts").glob("*.sh"):
            assert pattern not in path.read_text(encoding="utf-8"), path.name


def test_the_main_only_deployment_workflow_is_intact() -> None:
    """This refactor must not weaken the deployment path."""
    workflow = REPO_ROOT / ".github" / "workflows" / "deploy-main.yml"
    text = workflow.read_text(encoding="utf-8")
    assert 'workflows: ["CI"]' in text
    assert "branches: [main]" in text
    assert "id-token: write" in text
    assert 'AUTO_DEPLOY_ENABLED:-0}" != "1"' in text
    assert "vars.SSM_DEPLOY_DOCUMENT" in text
    assert "--document-version" in text


def test_the_fixed_ssm_document_is_intact() -> None:
    template = REPO_ROOT / "deploy" / "cloudformation" / "github-oidc.yaml"
    text = template.read_text(encoding="utf-8")
    assert "Name: RadioBroadcastDeployMain" in text
    assert "interpolationType: ENV_VAR" in text
    assert "merge-base --is-ancestor" in text


def test_rollback_remains_artifact_only() -> None:
    text = (REPO_ROOT / "scripts" / "rollback-compose.sh").read_text(encoding="utf-8")
    assert "--no-build" in text
    assert "--pull never" in text


def test_the_models_lock_is_unchanged_by_this_refactor() -> None:
    lock = json.loads((REPO_ROOT / "models.lock.json").read_text(encoding="utf-8"))
    assert set(lock["models"]) == {"asr.small", "llm.qwen3-0.6b", "vad.silero"}
