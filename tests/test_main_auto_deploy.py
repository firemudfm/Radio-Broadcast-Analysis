"""Main-only automatic deployment: triggers, first install, idempotency.

The properties under test are the ones that decide whether a bad change can
reach production, and whether a good one reaches it twice:

* **main is the only source.** No feature branch, pull request, tag or typed
  input can start a production deployment.
* **CI's verdict is the gate**, not the fact that a push happened.
* **Nothing arbitrary reaches the host.** One 40-hex SHA, one fixed SSM
  document, no shell-command parameter, no SSH, no static credential.
* **Idempotent by construction.** Every install action is guarded by a check, so
  a host that is already correct is left alone -- packages, Docker, Compose, the
  generated secret, and above all the models.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LIB = SCRIPTS / "lib" / "deploy-common.sh"
DEPLOY_WORKFLOW = WORKFLOWS / "deploy-main.yml"
TOOLCHAIN_LOCK = REPO_ROOT / "deploy" / "toolchain.lock.json"
CFN_TEMPLATE = REPO_ROOT / "deploy" / "cloudformation" / "github-oidc.yaml"
APP_ENV_TEMPLATE = REPO_ROOT / "deploy" / "env" / "application.env.example"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available on this host")

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_PRECONDITION = 65

SHA = "a1b2c3d4" * 5


def workflow_text() -> str:
    return DEPLOY_WORKFLOW.read_text(encoding="utf-8")


def workflow_yaml() -> str:
    """The workflow with YAML comment lines removed.

    The comments deliberately name the things they forbid ("must never use
    $LATEST", "NO inputs"), so a raw scan would match the explanation instead of
    a real setting.
    """
    return "\n".join(
        line for line in workflow_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def cfn_yaml() -> str:
    """The CloudFormation template with comment lines removed."""
    return "\n".join(
        line for line in CFN_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def executable_lines(path: Path) -> str:
    """Script text without comment lines.

    Comments legitimately NAME the things they forbid, so scanning raw text
    would match an explanation rather than a real call.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def run_script(path: Path, *args: str, **env) -> subprocess.CompletedProcess:
    environment = {**os.environ, **{k: str(v) for k, v in env.items()}}
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, str(path), *args],
        capture_output=True, text=True, timeout=180, check=False,
        cwd=str(REPO_ROOT), env=environment,
    )


# =============================================================================
# A. main is the only deployment source
# =============================================================================


def test_the_deploy_workflow_only_watches_main() -> None:
    text = workflow_text()
    assert "branches: [main]" in text
    assert 'workflows: ["CI"]' in text


def test_the_deploy_workflow_has_no_push_or_pull_request_trigger() -> None:
    """A push trigger would deploy before CI had a verdict; a pull_request
    trigger would deploy an unreviewed branch."""
    text = workflow_text()
    trigger_block = text[text.index("\non:"):text.index("permissions:")]
    for forbidden in ("push:", "pull_request:", "release:", "schedule:"):
        assert forbidden not in trigger_block, f"{forbidden} must not trigger production"
    assert "tags:" not in trigger_block, "a tag must never deploy"


def test_workflow_dispatch_accepts_no_inputs() -> None:
    """No branch, commit, stage or command may be typed in."""
    text = workflow_yaml()
    trigger_block = text[text.index("\non:"):text.index("permissions:")]
    assert "workflow_dispatch:" in trigger_block
    assert "inputs:" not in trigger_block, "manual dispatch must accept no input at all"


def test_manual_dispatch_rejects_a_non_main_ref() -> None:
    text = workflow_text()
    assert 'DISPATCH_REF}" != "refs/heads/main"' in text
    assert "Only refs/heads/main may deploy" in text


def test_workflow_run_requires_ci_success_push_and_main() -> None:
    """All three conditions are load-bearing and each must be present."""
    text = workflow_text()
    assert 'WORKFLOW_RUN_CONCLUSION}" != "success"' in text
    assert 'WORKFLOW_RUN_EVENT}" != "push"' in text
    assert 'WORKFLOW_RUN_BRANCH}" != "main"' in text


def test_the_exact_head_sha_of_the_ci_run_is_deployed() -> None:
    """Not `main` as of now -- the commit CI actually tested."""
    text = workflow_text()
    assert "github.event.workflow_run.head_sha" in text


def test_the_workflow_validates_the_commit_shape() -> None:
    text = workflow_text()
    assert "^[0-9a-f]{40}$" in text


# =============================================================================
# B. The auto-deploy enable gate
# =============================================================================


def test_deployment_is_skipped_unless_auto_deploy_is_exactly_one() -> None:
    """Merging the PR that adds this workflow must not deploy before the AWS
    document exists."""
    text = workflow_text()
    assert 'AUTO_DEPLOY_ENABLED:-0}" != "1"' in text
    assert "Skipping deployment. This is the safe default." in text


def test_every_aws_step_is_gated_on_the_deploy_decision() -> None:
    """An ungated AWS step would assume the role even on a skipped run."""
    text = workflow_text()
    for marker in (
        "uses: aws-actions/configure-aws-credentials",
        "aws ssm send-command",
        "aws sts get-caller-identity",
    ):
        index = text.index(marker)
        preceding = text[:index]
        step_start = preceding.rindex("      - name:")
        step_header = text[step_start:index]
        assert "steps.target.outputs.should_deploy == 'true'" in step_header, (
            f"step containing {marker!r} is not gated on should_deploy"
        )


# =============================================================================
# C. Nothing arbitrary reaches the host
# =============================================================================


def test_no_static_aws_credentials_anywhere_in_the_workflow() -> None:
    text = workflow_text()
    for forbidden in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "aws_access_key_id", "aws configure",
    ):
        assert forbidden not in text, f"{forbidden} must never appear"
    assert "id-token: write" in text, "OIDC is the only credential path"


def test_no_ssh_anywhere_in_the_workflow() -> None:
    text = workflow_text()
    for forbidden in ("EC2_SSH_KEY", "EC2_HOST", "ssh ", "scp ", "appleboy", "port 22"):
        assert forbidden not in text, f"{forbidden} must never appear"


def test_the_workflow_never_sends_an_arbitrary_command_document() -> None:
    text = workflow_text()
    assert "AWS-RunShellScript" not in text
    assert "AWS-RunPowerShellScript" not in text
    assert "vars.SSM_DEPLOY_DOCUMENT" in text, "only the fixed document is sent"


def test_the_document_version_is_explicit_and_numeric() -> None:
    """$LATEST would let a change to the SSM document alter what production runs
    with nothing in this repository changing."""
    text = workflow_yaml()
    assert "--document-version" in text
    assert "$LATEST" not in text
    assert "$DEFAULT" not in text
    assert 'expected}" =~ ^[0-9]+$' in text, "the pinned version must be numeric"
    assert "Document version drift" in text, "live version must be compared before sending"


def test_only_the_commit_sha_is_passed_as_a_parameter() -> None:
    text = workflow_text()
    parameters = re.findall(r'--parameters "([^"]+)"', text)
    assert parameters, "the workflow must pass a parameter"
    for spec in parameters:
        assert spec.startswith("CommitSha="), f"unexpected SSM parameter: {spec}"


def test_the_workflow_redacts_before_printing_host_output() -> None:
    text = workflow_text()
    assert "redact()" in text
    assert "RADIO_AUDIO_TOKEN_SECRET" in text, "the one secret must be redacted by name"


def test_production_environment_and_serialised_concurrency() -> None:
    text = workflow_text()
    assert "environment: production" in text
    assert "group: radio-production-main-deploy" in text
    assert "cancel-in-progress: false" in text, "never interrupt a half-finished deployment"


# =============================================================================
# D. Legacy workflows are gone
# =============================================================================


@pytest.mark.parametrize("name", ["deploy.yml", "health.yml", "rollback.yml", "oidc-claims.yml"])
def test_legacy_workflow_is_removed(name: str) -> None:
    assert not (WORKFLOWS / name).exists(), f"{name} is the old SSH/systemd path"


def test_only_the_intended_workflows_remain() -> None:
    present = sorted(p.name for p in WORKFLOWS.glob("*.yml"))
    assert present == ["ci.yml", "codeql.yml", "deploy-main.yml", "oidc-ssm-smoke.yml"]


def test_no_workflow_references_ssh_or_a_static_credential() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for forbidden in ("EC2_SSH_KEY", "EC2_HOST", "AWS_SECRET_ACCESS_KEY"):
            assert forbidden not in text, f"{workflow.name} references {forbidden}"


# =============================================================================
# E. The fixed SSM document and the IAM role
# =============================================================================


def cfn_text() -> str:
    return CFN_TEMPLATE.read_text(encoding="utf-8")


def test_the_deployment_document_exists_and_is_a_command_document() -> None:
    text = cfn_text()
    assert "Name: RadioBroadcastDeployMain" in text
    assert "DocumentType: Command" in text
    assert "schemaVersion: '2.2'" in text
    assert "TargetType: /AWS::EC2::Instance" in text
    assert "UpdateMethod: NewVersion" in text


def test_the_document_takes_only_a_pattern_constrained_commit() -> None:
    text = cfn_text()
    assert "CommitSha:" in text
    assert "allowedPattern: '^[0-9a-f]{40}$'" in text
    for forbidden in ("Branch:", "Tag:", "Stage:", "Command:", "Script:", "Packages:"):
        assert f"\n            {forbidden}" not in text, f"{forbidden} must not be a parameter"


def test_the_document_uses_env_var_interpolation() -> None:
    """With ENV_VAR the parameter can never be parsed as shell, so even a value
    that escaped the pattern cannot become a command."""
    text = cfn_text()
    assert "interpolationType: ENV_VAR" in text
    assert "SSM_CommitSha" in text


def test_the_document_never_pulls_resets_or_checks_out() -> None:
    text = cfn_text()
    executable = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("git pull", "git reset", "git checkout", "git merge "):
        assert forbidden not in executable, f"the document must never run {forbidden}"


def test_the_document_requires_the_commit_to_be_on_main() -> None:
    text = cfn_text()
    assert "merge-base --is-ancestor" in text
    assert "origin/main" in text


def test_the_document_validates_architecture_and_mount() -> None:
    text = cfn_text()
    assert "aarch64" in text
    assert "mountpoint -q" in text


def test_the_document_clones_only_when_absent_and_verifies_origin() -> None:
    text = cfn_text()
    assert 'if [ ! -d "${REPO_DIR}/.git" ]' in text
    assert "remote get-url origin" in text
    assert "existing clone points at" in text


def test_the_role_may_send_only_the_two_fixed_documents() -> None:
    text = cfn_text()
    assert "document/RadioBroadcastOidcSmoke" in text
    assert "document/RadioBroadcastDeployMain" in text
    # SendCommand must never be granted against every document.
    send_blocks = re.findall(r"Action: ssm:SendCommand\n\s+Resource:\n((?:\s+- .*\n)+)", text)
    assert send_blocks, "SendCommand must be granted explicitly"
    for block in send_blocks:
        assert "'*'" not in block, "SendCommand must never use Resource '*'"


def test_the_role_is_constrained_to_one_instance() -> None:
    text = cfn_text()
    assert "instance/${TargetInstanceId}" in text
    assert "AllowedPattern: '^i-[0-9a-f]{17}$'" in text


def test_the_role_denies_arbitrary_execution_and_escalation() -> None:
    text = cfn_text()
    for denied in (
        "document/AWS-RunShellScript",
        "ssm:StartSession",
        "ec2-instance-connect:SendSSHPublicKey",
        "iam:*",
        "secretsmanager:*",
    ):
        assert denied in text, f"{denied} must be explicitly denied"
    assert "iam:PassRole" not in text.replace("iam:*", ""), "PassRole must never be granted"


def test_the_trust_policy_pins_the_exact_live_subject() -> None:
    """The LIVE subject is environment-scoped, because the workflow runs with
    `environment: production`. A ref-scoped subject would simply stop the
    deployment authenticating. StringLike with a wildcard would let any branch,
    pull request or tag assume the role."""
    text = cfn_yaml()
    assert "'token.actions.githubusercontent.com:sub': !Ref GitHubOidcSubject" in text
    assert "repo:naman1995jain/Radio-Broadcast-Analysis:environment:production" in text
    assert "StringEquals:" in text
    assert "StringLike" not in text


def test_the_session_duration_is_capped_at_the_approved_value() -> None:
    text = cfn_text()
    assert "MaxValue: 7200" in text


def test_the_template_grants_no_data_plane_access() -> None:
    """S3 and SQS belong to the EC2 instance role, not to GitHub."""
    text = cfn_text()
    allow_section = text[:text.index("DenyArbitraryCommandExecution")]
    for forbidden in ("s3:", "sqs:", "AdministratorAccess", "PowerUserAccess"):
        assert forbidden not in allow_section, f"{forbidden} must not be granted to GitHub"


# =============================================================================
# F. The toolchain lock
# =============================================================================


def load_lock() -> dict:
    return json.loads(TOOLCHAIN_LOCK.read_text(encoding="utf-8"))


def test_the_toolchain_lock_has_the_expected_schema() -> None:
    lock = load_lock()
    assert lock["schema"] == "radio.toolchain.lock.v1"
    compose = lock["docker_compose"]
    assert compose["version"].startswith("v")
    asset = compose["linux_aarch64"]
    assert asset["asset"] == "docker-compose-linux-aarch64"
    assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]), "a real sha256 must be pinned"
    assert asset["install_path"].endswith("/docker-compose")


def test_the_toolchain_lock_contains_no_secret_or_account_identifier() -> None:
    text = TOOLCHAIN_LOCK.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text), "no AWS account id"
    assert not re.search(r"\bi-[0-9a-f]{8,17}\b", text), "no instance id"
    # Assembled at runtime: written as a literal, this test file would itself
    # trip the repository's private-key scanner.
    private_key_marker = "BEGIN " + "PRIVATE KEY"
    for forbidden in ("AKIA", "ASIA", private_key_marker, "SECRET", "password"):
        assert forbidden not in text


def test_the_package_manifest_matches_the_approved_set() -> None:
    lock = load_lock()
    required = set(lock["packages"]["required"])
    approved = {
        "git", "docker", "python3.11", "python3.11-pip", "python3.11-devel",
        "gcc", "gcc-c++", "make", "cmake", "ninja-build", "jq", "unzip", "tar",
        "rsync", "openssl", "sqlite", "xfsprogs", "nvme-cli", "htop", "tmux",
        "curl-minimal", "wget", "libgomp", "shadow-utils", "findutils", "which",
    }
    assert required == approved


def test_full_curl_is_forbidden_because_it_erases_curl_minimal() -> None:
    lock = load_lock()
    assert "curl" in lock["packages"]["forbidden"]
    assert "curl-minimal" in lock["packages"]["required"]
    assert "curl" not in set(lock["packages"]["required"])


def test_the_docker_data_root_is_on_the_data_volume() -> None:
    lock = load_lock()
    assert lock["docker_daemon"]["expected"]["data-root"] == "/var/lib/radio/docker"


# =============================================================================
# G. Host prerequisites: install only what is missing
# =============================================================================


PREREQ = SCRIPTS / "ensure-host-prerequisites.sh"


def test_packages_are_checked_before_being_installed() -> None:
    code = executable_lines(PREREQ)
    assert "rpm -q" in code, "each package must be checked first"
    assert 'dnf install -y "${MISSING[@]}"' in code, "only missing packages are installed"


def test_no_forbidden_dnf_flag_is_ever_used() -> None:
    code = executable_lines(PREREQ)
    for forbidden in ("--allowerasing", "--skip-broken", "--nogpgcheck", "--disablerepo"):
        assert forbidden not in code, f"{forbidden} must never be used"


def test_no_package_is_upgraded_during_a_deployment() -> None:
    """An upgrade command may appear in a remediation STRING the operator runs
    themselves; it may never be executed by the deployment."""
    for line in executable_lines(PREREQ).splitlines():
        for forbidden in ("dnf upgrade", "dnf update", "yum update"):
            if forbidden not in line:
                continue
            assert line.lstrip().startswith("remediation "), (
                f"{forbidden} would apply unreviewed change: {line.strip()}"
            )


def test_docker_is_only_started_and_never_reinstalled_when_present() -> None:
    code = executable_lines(PREREQ)
    assert "systemctl is-enabled docker" in code
    assert "systemctl is-active docker" in code
    assert "DockerRootDir" in code, "the running data-root must be verified"


def test_an_existing_daemon_config_is_never_edited() -> None:
    """Rewriting data-root would orphan every existing image and container."""
    code = executable_lines(PREREQ)
    assert "conflicting Docker daemon configuration" in code
    assert 'if [ ! -f "${DOCKER_CONFIG}" ]' in code


def test_compose_is_verified_by_digest_and_skipped_when_correct() -> None:
    code = executable_lines(PREREQ)
    assert "compose_is_correct" in code
    assert "sha256sum" in code
    assert "checksum mismatch" in code
    assert "unverifiable docker compose binary" in code


def test_the_radio_account_is_never_added_to_the_docker_group() -> None:
    """Docker group membership is equivalent to root."""
    code = executable_lines(PREREQ)
    assert "usermod" not in code
    assert "gpasswd" not in code


def test_prerequisites_never_touch_a_block_device() -> None:
    code = executable_lines(PREREQ)
    # `mountpoint -q` is a read-only test and is expected; an actual `mount`
    # of a device, or any formatting tool, is not.
    for forbidden in ("mkfs", "parted", "fdisk", "sgdisk", "wipefs", "dd if=",
                      "mount -o", "mount /dev", "mount -t"):
        assert forbidden not in code, f"{forbidden} must never appear in a deployment path"


def test_prerequisites_refuse_a_non_aarch64_host() -> None:
    result = run_script(PREREQ, "--dry-run", RADIO_SKIP_MOUNT_CHECK="1")
    # This test host is x86_64, so the architecture gate must fire.
    if os.uname().machine != "aarch64" if hasattr(os, "uname") else True:
        assert result.returncode != EXIT_OK
        assert "aarch64" in result.stderr


# =============================================================================
# H. Production configuration: create once, never overwrite
# =============================================================================


CONFIG = SCRIPTS / "ensure-production-config.sh"


def test_infrastructure_env_is_never_generated() -> None:
    """It names the real bucket and queues; a guess would point the pipeline at
    infrastructure that is not there."""
    code = executable_lines(CONFIG)
    assert "must be provisioned by an operator, never guessed" in code


def test_application_env_is_created_only_when_absent() -> None:
    code = executable_lines(CONFIG)
    assert 'if [ -f "${APPLICATION}" ]' in code
    assert "preserving it byte-for-byte" in code


def test_the_audio_token_secret_is_generated_once_with_a_csprng() -> None:
    code = executable_lines(CONFIG)
    assert "secrets.token_urlsafe(48)" in code, "48 bytes from the OS CSPRNG"
    assert 'REPLACE_WITH_GENERATED_SECRET' in CONFIG.read_text(encoding="utf-8")


def test_the_generated_secret_is_never_printed() -> None:
    code = executable_lines(CONFIG)
    assert "value not printed" in CONFIG.read_text(encoding="utf-8")
    assert "echo ${secret}" not in code
    assert 'echo "${secret}"' not in code


def test_the_secret_file_is_created_with_restrictive_mode_before_writing() -> None:
    """A file created 0644 and chmodded later is briefly world-readable."""
    code = executable_lines(CONFIG)
    assert "install -m 0640" in code


def test_compose_env_is_created_only_when_absent_and_then_validated() -> None:
    code = executable_lines(CONFIG)
    assert 'if [ -f "${COMPOSE_ENV}" ]' in code
    assert "validate_publish_host" in code


def test_direct_http_exposure_requires_explicit_acknowledgement() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    assert "RADIO_ALLOW_DIRECT_HTTP=1" in text
    assert "security group is the ONLY thing restricting access" in text


def test_the_config_script_never_modifies_a_security_group() -> None:
    code = executable_lines(CONFIG)
    for forbidden in ("aws ec2", "authorize-security-group", "modify-instance"):
        assert forbidden not in code


def test_station_capacity_above_the_reviewed_ceiling_is_rejected() -> None:
    code = executable_lines(CONFIG)
    assert "RADIO_MAX_ALLOWED_STATION_CAPACITY" in code
    assert "exceeds the reviewed ceiling" in code


def test_configuration_validation_is_split_into_two_layers() -> None:
    """Layer A is stdlib-only on the host: bare python has no pydantic, and
    importing from the source working tree would validate whatever is checked
    out rather than the commit being deployed. Layer B runs the real Settings
    model inside the exact-SHA image."""
    host_side = executable_lines(CONFIG)
    assert "from app.config import Settings" not in host_side
    assert "structural checks passed" in host_side
    assert "uvicorn" not in host_side, "validation must not start the API"

    deploy = executable_lines(SCRIPTS / "deploy-compose.sh")
    assert "app.cli.validate_configuration" in deploy
    assert '"${RADIO_API_IMAGE}"' in deploy


# =============================================================================
# I. The application template
# =============================================================================


def parse_env_template() -> dict[str, str]:
    values = {}
    for line in APP_ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_every_template_setting_exists_in_the_settings_model() -> None:
    """A setting that does not exist is silently ignored, which is worse than
    being absent -- it reads as configured when it is not."""
    from app.config import Settings

    known = set(Settings.model_fields)
    for name in parse_env_template():
        assert name in known, f"{name} is not a real setting in app/config.py"


def test_the_template_sets_the_pilot_capacity_to_one() -> None:
    values = parse_env_template()
    assert values["RADIO_MAX_ACTIVE_UNIQUE_STATIONS"] == "1"
    assert values["RADIO_LISTENER_MAX_SESSIONS"] == "1"


def test_the_template_configures_the_only_pipeline() -> None:
    """There is no mode to select any more; the template must not carry one."""
    values = parse_env_template()
    assert "RADIO_PIPELINE_MODE" not in values, "the mode switch was removed"
    assert values["RADIO_QUEUE_BACKEND"] == "sqs"
    assert values["RADIO_SEGMENT_STORE"] == "local"


def test_the_template_states_both_capacity_numbers() -> None:
    """Requested is a control-plane bound; active is compute. Reading one as
    the other is how "1,000 stations" becomes a claim about live decoding."""
    values = parse_env_template()
    assert values["RADIO_MAX_REQUESTED_UNIQUE_STATIONS"] == "1000"
    assert values["RADIO_MAX_ACTIVE_UNIQUE_STATIONS"] == "1"


def test_the_template_keeps_speech_over_music() -> None:
    """Discarding it would drop most real brand mentions."""
    values = parse_env_template()
    assert values["RADIO_INCLUDE_SPEECH_OVER_MUSIC"] == "true"
    assert values["RADIO_INCLUDE_SONG_LYRICS"] == "false"
    assert values["RADIO_TRANSCRIBE_UNCERTAIN_AUDIO"] == "true"


def test_the_template_contains_no_real_secret() -> None:
    values = parse_env_template()
    assert values["RADIO_AUDIO_TOKEN_SECRET"] == "REPLACE_WITH_GENERATED_SECRET"


def test_the_template_makes_no_thousand_station_claim() -> None:
    text = APP_ENV_TEMPLATE.read_text(encoding="utf-8")
    assert "1,000" in text or "1000" in text, "the limitation should be stated"
    assert "no evidence this host supports anything near 1,000" in text


# =============================================================================
# J. Models: verify first, download only what is missing
# =============================================================================


MODELS = SCRIPTS / "ensure-models.sh"


def test_models_are_verified_before_any_download() -> None:
    code = executable_lines(MODELS)
    assert "verify_role" in code
    verify_index = code.index("if verify_role")
    download_index = code.index("download-models.py")
    assert verify_index < download_index, "verification must come first"


def test_a_verified_model_is_never_downloaded_again() -> None:
    text = MODELS.read_text(encoding="utf-8")
    assert "already present; not downloading" in text
    # The verified branch must `continue`, never fall through to a download.
    code = executable_lines(MODELS)
    verified_branch = code[code.index("if verify_role"):code.index("presence=")]
    assert "continue" in verified_branch


def test_an_existing_model_that_fails_verification_fails_closed(tmp_path: Path) -> None:
    """A truncated or tampered model is evidence. Silently replacing it hides
    both the cause and the fact that the system ran on something unverified."""
    code = executable_lines(MODELS)
    assert "refusing to overwrite or delete an existing model that does not verify" in code
    assert "rm -rf" not in code, "a failing model must never be deleted automatically"


def test_models_are_only_taken_from_the_lock() -> None:
    code = executable_lines(MODELS)
    assert "is not pinned in" in code
    assert "download-models.py" in code, "downloads go through the pinned fetcher"


def test_the_model_script_never_logs_a_token_or_presigned_url() -> None:
    code = executable_lines(MODELS)
    for forbidden in ("HF_TOKEN", "Authorization:", "X-Amz-Signature"):
        assert forbidden not in code


def test_the_model_script_supports_the_required_modes() -> None:
    text = MODELS.read_text(encoding="utf-8")
    for flag in ("--all", "--verify-only", "--dry-run", "--role"):
        assert flag in text


def test_a_dry_run_downloads_nothing(tmp_path: Path) -> None:
    result = run_script(
        MODELS, "--all", "--dry-run", "--root", str(tmp_path / "models"),
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "would download" in result.stdout
    assert not list((tmp_path / "models").rglob("*.bin"))
    assert not list((tmp_path / "models").rglob("*.gguf"))


def test_an_unknown_role_is_refused() -> None:
    result = run_script(MODELS, "--role", "everything")
    assert result.returncode == EXIT_USAGE


def test_no_container_downloads_a_model_at_startup() -> None:
    """An implicit download turns a routine restart into an outage when the
    provider is unreachable, with no operator watching."""
    for dockerfile in (REPO_ROOT / "docker").glob("*.Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        for forbidden in ("download-models.py", "huggingface-cli download", "hf_hub_download"):
            assert forbidden not in text, f"{dockerfile.name} downloads a model"
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "download-models" not in compose


# =============================================================================
# K. main-auto-deploy: arguments, mode selection, ordering
# =============================================================================


AUTO = SCRIPTS / "main-auto-deploy.sh"


def test_auto_deploy_accepts_exactly_two_arguments() -> None:
    text = AUTO.read_text(encoding="utf-8")
    assert "--commit)" in text and "--source-repo)" in text
    for forbidden in ("--stage)", "--branch)", "--command)", "--tag)", "--packages)"):
        assert forbidden not in text, f"{forbidden} must not be accepted"


def test_auto_deploy_rejects_an_unknown_argument() -> None:
    result = run_script(AUTO, "--commit", SHA, "--source-repo", "/tmp", "--stage", "full")
    assert result.returncode == EXIT_USAGE
    assert "unexpected argument" in result.stderr


@pytest.mark.parametrize("value", ["main", "HEAD", "v1.0", "a1b2c3d", SHA.upper(), ""])
def test_auto_deploy_rejects_anything_but_a_full_sha(value: str) -> None:
    result = run_script(AUTO, "--commit", value, "--source-repo", "/var/lib/radio/app")
    assert result.returncode == EXIT_USAGE


def test_auto_deploy_requires_an_absolute_source_repo() -> None:
    result = run_script(AUTO, "--commit", SHA, "--source-repo", "relative/path")
    assert result.returncode == EXIT_USAGE
    assert "absolute" in result.stderr


def test_auto_deploy_never_fetches_git_itself() -> None:
    """The SSM document has already placed the exact commit on disk. A fetch
    here would be a second, unreviewed way for content to arrive."""
    code = executable_lines(AUTO)
    for forbidden in ("git fetch", "git pull", "git clone", "git reset", "git checkout"):
        assert forbidden not in code, f"main-auto-deploy must never run {forbidden}"


def test_auto_deploy_requires_the_commit_to_be_on_main() -> None:
    code = executable_lines(AUTO)
    assert "merge-base --is-ancestor" in code
    assert "origin/main" in code


def test_auto_deploy_verifies_the_repository_origin() -> None:
    code = executable_lines(AUTO)
    assert "remote get-url origin" in code
    assert "refusing to deploy from an unexpected source" in code


def test_auto_deploy_uses_a_separate_high_level_lock() -> None:
    """Sharing the deployment lock would deadlock the moment deploy-compose.sh
    took it."""
    code = executable_lines(AUTO)
    assert "RADIO_INSTALL_LOCK" in code
    assert "flock -n 8" in code, "a distinct descriptor from the deployment lock"


def test_a_partial_state_file_is_not_treated_as_a_deployment() -> None:
    """Otherwise a killed process would skip first install and deploy straight
    to full on a host with no models."""
    code = executable_lines(AUTO)
    assert "validate_release_manifest" in code
    assert "treating this as a FIRST INSTALL" in AUTO.read_text(encoding="utf-8")


def test_first_install_runs_api_then_core_then_full_in_order() -> None:
    text = AUTO.read_text(encoding="utf-8")
    first_install = text[text.index('if [ "${MODE}" = "first-install" ]'):text.index("else\n    stage \"8/9")]
    api = first_install.index("deploy_stage api")
    core = first_install.index("deploy_stage core")
    full = first_install.index("deploy_stage full")
    assert api < core < full, "stages must widen one at a time"


def test_a_normal_update_deploys_full_directly() -> None:
    text = AUTO.read_text(encoding="utf-8")
    update = text[text.index('stage "8/9  Update'):]
    assert "deploy_stage full" in update
    assert "deploy_stage api" not in update
    assert "deploy_stage core" not in update


def test_first_install_fetches_all_models_and_updates_verify_only() -> None:
    text = AUTO.read_text(encoding="utf-8")
    first_install = text[text.index('if [ "${MODE}" = "first-install" ]'):text.index('stage "8/9  Update')]
    update = text[text.index('stage "8/9  Update'):]
    assert "ensure-models.sh\" --all" in first_install
    assert "--verify-only" in update


def test_auto_deploy_never_restores_the_database_or_touches_a_disk() -> None:
    code = executable_lines(AUTO)
    for forbidden in ("mkfs", "parted", "fdisk", "mount ", "sqlite3 .restore", "docker system prune"):
        assert forbidden not in code, f"{forbidden} must never appear"


def test_auto_deploy_delegates_recovery_to_the_artifact_only_path() -> None:
    """Rollback stays build-free and pull-free; this script adds no second
    recovery mechanism of its own."""
    code = executable_lines(AUTO)
    assert "deploy-compose.sh" in code
    assert "rollback-compose.sh" not in code, "recovery is deploy-compose.sh's own trap"


def test_auto_deploy_emits_a_machine_readable_success_marker() -> None:
    text = AUTO.read_text(encoding="utf-8")
    assert "MAIN_AUTO_DEPLOY_OK" in text
    assert "MAIN_AUTO_DEPLOY_OK" in workflow_text(), "the workflow must assert it"


def test_auto_deploy_never_prints_an_environment_file() -> None:
    code = executable_lines(AUTO)
    for forbidden in ("cat ${ENV_DIR}", 'cat "${ENV_DIR}', "cat /etc/radio"):
        assert forbidden not in code


# =============================================================================
# L. Idempotency of the whole path
# =============================================================================


IDEMPOTENT_SCRIPTS = [
    SCRIPTS / "ensure-host-prerequisites.sh",
    SCRIPTS / "ensure-production-config.sh",
    SCRIPTS / "ensure-models.sh",
    SCRIPTS / "main-auto-deploy.sh",
]


@pytest.mark.parametrize("script", IDEMPOTENT_SCRIPTS, ids=lambda p: p.name)
def test_every_install_script_parses(script: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, "-n", str(script)], capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", IDEMPOTENT_SCRIPTS, ids=lambda p: p.name)
def test_every_install_script_supports_help(script: Path) -> None:
    result = run_script(script, "--help")
    assert result.returncode == EXIT_OK, result.stderr


@pytest.mark.parametrize("script", IDEMPOTENT_SCRIPTS, ids=lambda p: p.name)
def test_no_install_script_creates_a_static_aws_credential(script: Path) -> None:
    code = executable_lines(script)
    for forbidden in ("aws configure", "AWS_SECRET_ACCESS_KEY=", "aws iam create-access-key"):
        assert forbidden not in code, f"{script.name} would create a static credential"


@pytest.mark.parametrize("script", IDEMPOTENT_SCRIPTS, ids=lambda p: p.name)
def test_no_install_script_uses_ssh(script: Path) -> None:
    code = executable_lines(script)
    for forbidden in ("ssh ", "scp ", "ssh-keygen", "authorized_keys"):
        assert forbidden not in code, f"{script.name} references {forbidden}"


@pytest.mark.parametrize("script", IDEMPOTENT_SCRIPTS, ids=lambda p: p.name)
def test_no_install_script_formats_or_mounts_a_device(script: Path) -> None:
    code = executable_lines(script)
    for forbidden in ("mkfs", "parted", "fdisk", "sgdisk", "wipefs", "mount -o", "mount /dev"):
        assert forbidden not in code, f"{script.name} would touch a block device"
