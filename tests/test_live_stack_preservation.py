"""Preserve the live OIDC stack; harden the first-deployment path.

The template in this repository is an *extension* of a stack that already
exists and already works. That makes identity the whole game: CloudFormation
keys resources by logical ID, so renaming one is not a rename — it is a DELETE
and a CREATE. For an IAM role that means the live role is destroyed and a new
one appears with no trust relationship anyone has approved; for the OIDC
provider it means every workflow stops authenticating.

The other corrections here all share a shape: something that looked right in
isolation was wrong against the real host. A ref-scoped trust subject cannot
authenticate a workflow that runs with `environment: production`. Requiring
Docker before running the script that installs Docker cannot complete a first
install. Streaming a build log through SSM cannot deliver the marker the
workflow looks for, because SSM truncates at 24,000 characters.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
CFN = REPO_ROOT / "deploy" / "cloudformation" / "github-oidc.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-main.yml"
LOCK = REPO_ROOT / "deploy" / "toolchain.lock.json"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available on this host")

EXIT_OK = 0
EXIT_PRECONDITION = 65


def cfn_text() -> str:
    return CFN.read_text(encoding="utf-8")


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def cfn_yaml() -> str:
    """Template with comment lines removed.

    The template explains what it must NOT contain ("No ThumbprintList",
    "never a ref:refs/heads/main form"), so a raw scan matches the explanation
    rather than a real setting.
    """
    return "\n".join(
        line for line in cfn_text().splitlines() if not line.lstrip().startswith("#")
    )


def workflow_yaml() -> str:
    return "\n".join(
        line for line in workflow_text().splitlines() if not line.lstrip().startswith("#")
    )


def deploy_document() -> str:
    """Only the DeployMainDocument resource.

    `mainSteps:` and `parameters:` appear at the same indentation in the smoke
    document, so an unscoped index finds that one instead.
    """
    text = cfn_yaml()
    return text[text.index("  DeployMainDocument:"):text.index("  GitHubActionsRole:")]


def cfn_resources() -> str:
    text = cfn_yaml()
    return text[text.index("\nResources:"):text.index("\nOutputs:")]


def executable_lines(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def run_snippet(snippet: str, **env):
    import os
    import textwrap
    lib = SCRIPTS / "lib" / "deploy-common.sh"
    script = f'set -euo pipefail\nsource "{lib.as_posix()}"\n{textwrap.dedent(snippet)}'
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array
        [BASH, "-c", script],
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, **{k: str(v) for k, v in env.items()}},
    )


# =============================================================================
# A. Live CloudFormation identities
# =============================================================================


LIVE_LOGICAL_IDS = ["GitHubOidcProvider", "OidcSmokeDocument", "GitHubActionsRole"]


@pytest.mark.parametrize("logical_id", LIVE_LOGICAL_IDS)
def test_the_live_logical_id_is_preserved(logical_id: str) -> None:
    """CloudFormation keys resources by logical ID. Renaming one DELETES the
    live resource and creates a replacement nobody approved."""
    assert re.search(rf"^  {logical_id}:$", cfn_text(), re.MULTILINE), (
        f"{logical_id} must keep its live logical ID"
    )


@pytest.mark.parametrize(
    "wrong_id", ["RadioBroadcastOidcSmoke:", "GitHubActionsRadioDeployRole:"]
)
def test_a_physical_name_is_never_used_as_a_logical_id(wrong_id: str) -> None:
    for line in cfn_text().splitlines():
        if line.startswith("  ") and not line.startswith("    "):
            assert line.strip() != wrong_id, f"{wrong_id} is a physical name, not a logical ID"


def test_the_physical_names_are_unchanged() -> None:
    text = cfn_text()
    assert "Name: RadioBroadcastOidcSmoke" in text
    assert "RoleName: GitHubActionsRadioDeployRole" in text
    assert "Name: RadioBroadcastDeployMain" in text


def test_only_one_new_resource_is_added() -> None:
    """The change set must be: add one document, modify one role."""
    logical_ids = re.findall(r"^  ([A-Za-z0-9]+):$", cfn_resources(), re.MULTILINE)
    assert set(logical_ids) == set(LIVE_LOGICAL_IDS) | {"DeployMainDocument"}


def test_the_existing_inline_policy_name_is_preserved() -> None:
    """Renaming an inline policy deletes the live one and creates another."""
    text = cfn_text()
    assert "PolicyName: GitHubActionsRadioOidcSmokeAccess" in text
    assert "RadioFixedSsmDocumentsOnly" not in text


def test_existing_resources_retain_their_retain_policies() -> None:
    text = cfn_text()
    assert text.count("DeletionPolicy: Retain") >= 2
    assert text.count("UpdateReplacePolicy: Retain") >= 2


def test_the_template_is_marked_as_pending_live_reconciliation() -> None:
    """The live stack has 13 outputs and 5 tags whose names are not known here.
    Inventing them would delete the real ones, so the file must refuse to
    present itself as ready to execute."""
    text = cfn_text()
    assert "PENDING LIVE RECONCILIATION" in text
    assert "DO NOT CREATE A CHANGE SET FROM THIS FILE YET" in text
    assert "13 Outputs" in text
    assert "RECONCILIATION REQUIRED" in text, "the Outputs section must say so too"


def test_the_template_documents_how_to_retrieve_the_live_baseline() -> None:
    text = cfn_text()
    assert "get-template" in text
    assert "--template-stage Original" in text


# =============================================================================
# B. Trust subject and provider
# =============================================================================


def test_the_trust_subject_is_the_live_environment_form() -> None:
    """The workflow runs with `environment: production`, so GitHub issues an
    environment-scoped subject. A ref-scoped trust policy would simply stop the
    deployment authenticating."""
    text = cfn_text()
    assert (
        "repo:naman1995jain/Radio-Broadcast-Analysis:environment:production" in text
    )
    trust = text[text.index("      AssumeRolePolicyDocument:"):text.index("      Policies:")]
    assert "ref:refs/heads/main" not in trust, "the live subject is environment-scoped"


def test_the_trust_subject_comes_from_a_parameter() -> None:
    text = cfn_text()
    assert "GitHubOidcSubject:" in text
    assert "token.actions.githubusercontent.com:sub: !Ref GitHubOidcSubject" in text


def test_the_trust_policy_uses_string_equals_without_a_wildcard() -> None:
    text = cfn_yaml()
    assert "StringEquals:" in text
    assert "StringLike" not in text


def test_the_provider_declares_no_thumbprint_list() -> None:
    """The live provider was created without one. Adding thumbprints produces an
    UpdateOpenIDConnectProviderThumbprint action on a resource this change is
    not supposed to touch."""
    text = cfn_yaml()
    assert "ThumbprintList" not in text
    assert "6938fd4d98bab03faadb97b34396831e3780aea1" not in text
    assert "1c58a3a8518e8759bf075b76b750d4f2df264fcd" not in text


def test_the_provider_url_and_client_id_are_unchanged() -> None:
    text = cfn_text()
    assert "Url: https://token.actions.githubusercontent.com" in text
    assert "- sts.amazonaws.com" in text


def test_the_template_reminds_that_tags_must_be_reconciled() -> None:
    """Omitting tags on an update REMOVES them."""
    text = cfn_text()
    assert text.count("reconcile the 5 live project tags") >= 2


# =============================================================================
# C. The smoke document is untouched
# =============================================================================


LIVE_SMOKE_LINES = [
    "set -euo pipefail",
    "echo GITHUB_OIDC_SSM_OK",
    'echo "user=$(whoami)"',
    'echo "architecture=$(uname -m)"',
    'test "$(uname -m)" = "aarch64"',
    "test -d /var/lib/radio",
    "test -d /var/lib/radio/app/Radio-Broadcast-Analysis",
    "test -f /etc/radio-broadcast-analysis/infrastructure.env",
    'test "$(systemctl is-active amazon-ssm-agent)" = "active"',
    "echo SSM_FIXED_DOCUMENT_OK",
]


@pytest.mark.parametrize("line", LIVE_SMOKE_LINES)
def test_the_smoke_document_content_matches_the_live_document(line: str) -> None:
    """Any difference creates a new document version during an update that is
    meant to leave this resource completely alone."""
    assert line in cfn_text(), f"live smoke line missing: {line}"


def test_the_smoke_document_was_not_shortened() -> None:
    text = cfn_text()
    smoke = text[text.index("OidcSmokeDocument:"):text.index("DeployMainDocument:")]
    for line in LIVE_SMOKE_LINES:
        assert line in smoke


# =============================================================================
# D. SSM parameter interpolation
# =============================================================================


def test_interpolation_type_is_declared_on_the_parameter() -> None:
    text = deploy_document()
    parameters_block = text[text.index("          CommitSha:"):text.index("        mainSteps:")]
    assert "interpolationType: ENV_VAR" in parameters_block


def test_interpolation_type_is_not_declared_on_step_inputs() -> None:
    """It is a parameter property. On a step's inputs it is simply ignored, and
    the value would then be substituted into the command text after all."""
    steps_block = deploy_document()
    steps_block = steps_block[steps_block.index("        mainSteps:"):]
    assert "interpolationType" not in steps_block


def test_the_command_reads_only_the_environment_variable() -> None:
    text = cfn_yaml()
    assert "${SSM_CommitSha:-}" in text
    assert "{{ CommitSha }}" not in text
    assert "{{CommitSha}}" not in text


def test_there_is_exactly_one_document_parameter() -> None:
    text = deploy_document()
    parameters_block = text[text.index("        parameters:"):text.index("        mainSteps:")]
    declared = re.findall(r"^          ([A-Za-z][A-Za-z0-9]*):$", parameters_block, re.MULTILINE)
    assert declared == ["CommitSha"]


# =============================================================================
# E. Bounded SSM output
# =============================================================================


def test_the_document_writes_a_local_log_rather_than_streaming() -> None:
    """SSM truncates StandardOutputContent at 24,000 characters, and a first
    install produces far more -- so streaming would cut off the very marker the
    workflow checks for."""
    text = cfn_yaml()
    assert 'LOG_DIR="${DATA_ROOT}/logs/deployments"' in text
    assert 'install -m 0600 -o root -g root /dev/null "${log_file}"' in text
    assert '>> "${log_file}" 2>&1' in text


def test_the_success_path_prints_only_a_concise_result() -> None:
    text = cfn_text()
    for marker in ("MAIN_AUTO_DEPLOY_OK", "deploy_log=", "deploy_exit_code=0"):
        assert marker in text


def test_the_failure_path_is_bounded_and_redacted() -> None:
    text = cfn_text()
    assert "tail -n 80" in text
    assert "head -c 6000" in text, "stderr must stay well below the 8,000 limit"
    assert "cut -c1-200" in text, "one pathological line must not consume the budget"
    assert "RADIO_AUDIO_TOKEN_SECRET=" in text, "the secret is redacted by name"
    assert "<redacted-key-id>" in text


def test_the_failure_path_returns_the_real_exit_code() -> None:
    text = cfn_text()
    assert 'exit "${status}"' in text


def test_the_log_directory_is_root_owned_and_the_file_is_0600() -> None:
    text = cfn_text()
    assert 'install -d -m 0750 -o root -g root "${LOG_DIR}"' in text
    assert "-m 0600" in text


def test_a_huge_deployment_log_still_yields_the_concise_marker(tmp_path: Path) -> None:
    """Simulates >24,000 characters of build output and proves the document's
    success path emits a short result rather than the log."""
    log_file = tmp_path / "deploy.log"
    noise = "\n".join(f"#{n} building layer {'x' * 60}" for n in range(1200))
    assert len(noise) > 24_000
    log_file.write_text(noise + "\nMAIN_AUTO_DEPLOY_OK\n", encoding="utf-8")

    result = run_snippet(
        f'''
        log_file="{log_file.as_posix()}"
        commit="{"a" * 40}"
        status=0
        if [ "${{status}}" -eq 0 ] && grep -q '^MAIN_AUTO_DEPLOY_OK$' "${{log_file}}"; then
            echo "MAIN_AUTO_DEPLOY_OK"
            echo "commit=${{commit}}"
            echo "deploy_log=${{log_file}}"
            echo "deploy_exit_code=0"
        fi
        '''
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "MAIN_AUTO_DEPLOY_OK" in result.stdout
    assert len(result.stdout) < 24_000
    assert "building layer" not in result.stdout, "the build log must not be streamed"


def test_the_workflow_asserts_the_marker_rather_than_assuming_it() -> None:
    text = workflow_text()
    assert "did not report its success marker" in text
    assert "Output length:" in text


# =============================================================================
# F. Timeout headroom
# =============================================================================


def timeouts() -> dict[str, int]:
    workflow = workflow_text()
    cfn = cfn_text()
    return {
        "role": int(re.search(r"role-duration-seconds: (\d+)", workflow).group(1)),
        "send": int(re.search(r"--timeout-seconds (\d+)", workflow).group(1)),
        "poll": int(re.search(r"deadline=\$\(\( SECONDS \+ (6\d{3}) \)\)", workflow).group(1)),
        "document": int(re.search(r"timeoutSeconds: '(6\d{3})'", cfn).group(1)),
    }


def test_the_role_outlives_every_timeout_beneath_it() -> None:
    """Using 7200 for all four means the credentials expire at the exact moment
    the last GetCommandInvocation needs them."""
    t = timeouts()
    assert t["role"] > t["poll"], "role duration must exceed the polling deadline"
    assert t["role"] > t["send"], "role duration must exceed the SendCommand timeout"
    assert t["role"] > t["document"], "role duration must exceed document execution"


def test_the_timeout_ladder_is_strictly_ordered() -> None:
    t = timeouts()
    assert t["role"] > t["poll"] > t["send"] > t["document"], t


def test_the_role_duration_is_capped_at_the_approved_value() -> None:
    assert timeouts()["role"] <= 7200
    assert "MaxValue: 7200" in cfn_text()


# =============================================================================
# G. Every protected check gates the deployment
# =============================================================================


REQUIRED_CONTEXTS = [
    "Lint (ruff)",
    "Tests (Python 3.11)",
    "Tests (Python 3.12)",
    "Security (bandit + pip-audit)",
    "Analyze Python",
]


@pytest.mark.parametrize("context", REQUIRED_CONTEXTS)
def test_the_workflow_requires_this_check_context(context: str) -> None:
    assert f'"{context}"' in workflow_text(), f"{context} must gate the deployment"


def test_codeql_is_verified_separately_from_ci() -> None:
    """CodeQL is its own workflow, so a workflow_run trigger from CI fires while
    Analyze Python may still be queued -- or already failed."""
    text = workflow_text()
    assert "Analyze Python" in text
    assert "check-runs" in text
    assert "checks: read" in text


def test_a_pending_check_blocks_and_then_fails_rather_than_deploying() -> None:
    text = workflow_text()
    assert "Waiting for required checks" in text
    assert "Required checks did not complete in time" in text


def test_an_unsuccessful_conclusion_of_any_kind_blocks() -> None:
    """skipped, cancelled, timed_out and neutral are refusals too: a check that
    did not actually run has not passed."""
    text = workflow_text()
    assert 'conclusion}" != "success"' in text
    assert "A required check did not succeed" in text


def test_a_missing_check_run_blocks() -> None:
    assert "MISSING" in workflow_text()


def test_the_check_gate_runs_before_any_aws_step() -> None:
    text = workflow_text()
    gate = text.index("Require every protected check")
    aws = text.index("uses: aws-actions/configure-aws-credentials")
    assert gate < aws, "checks must be confirmed before credentials are assumed"


def test_the_check_gate_does_not_expose_the_token() -> None:
    text = workflow_text()
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "echo ${GH_TOKEN}" not in text
    assert 'echo "${GH_TOKEN}"' not in text


# =============================================================================
# H. SSM Agent minimum
# =============================================================================


def load_lock() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def test_the_minimum_ssm_agent_version_is_pinned() -> None:
    lock = load_lock()
    assert lock["minimum_versions"]["ssm_agent"] == "3.3.2746.0"
    assert lock["minimum_versions"]["ssm_agent_verified_in_production"] == "3.3.4624.0"


@pytest.mark.parametrize(
    ("have", "accepted"),
    [
        ("3.3.2746.0", True),    # exactly the minimum
        ("3.3.4624.0", True),    # the version actually in production
        ("3.4.0.0", True),
        ("4.0.0.0", True),
        ("3.3.2745.0", False),   # one patch below
        ("3.3.2745.999", False),
        ("3.2.9999.0", False),
        ("2.9.9.9", False),
    ],
)
def test_version_comparison_is_numeric_not_lexical(have: str, accepted: bool) -> None:
    """'3.3.4624.0' sorts BEFORE '3.3.2746.0' as a string, so a lexical
    comparison would reject the newer agent that is actually running."""
    result = run_snippet(
        f'''
        version_at_least() {{
            local have="$1" want="$2" i
            local -a h w
            IFS='.' read -r -a h <<<"${{have}}"
            IFS='.' read -r -a w <<<"${{want}}"
            for i in 0 1 2 3; do
                local hv="${{h[i]:-0}}" wv="${{w[i]:-0}}"
                case "${{hv}}${{wv}}" in
                    *[!0-9]*) return 2 ;;
                esac
                if [ "${{hv}}" -gt "${{wv}}" ]; then return 0; fi
                if [ "${{hv}}" -lt "${{wv}}" ]; then return 1; fi
            done
            return 0
        }}
        if version_at_least "{have}" "3.3.2746.0"; then echo ACCEPTED; else echo REJECTED; fi
        '''
    )
    expected = "ACCEPTED" if accepted else "REJECTED"
    assert expected in result.stdout, f"{have}: {result.stdout}{result.stderr}"


def test_a_malformed_agent_version_is_rejected() -> None:
    code = executable_lines(SCRIPTS / "ensure-host-prerequisites.sh")
    assert "is not a dotted numeric version" in code
    assert "unsupported SSM Agent version" in code


def test_the_agent_is_never_upgraded_automatically() -> None:
    """An unreviewed agent version arriving mid-deployment is exactly the
    unreviewed change this whole design refuses."""
    executed = [
        line for line in executable_lines(SCRIPTS / "ensure-host-prerequisites.sh").splitlines()
        if "dnf update amazon-ssm-agent" in line
    ]
    assert executed, "the remediation must at least tell the operator what to run"
    for line in executed:
        assert line.lstrip().startswith("remediation "), (
            f"the agent must never be upgraded by the deployment itself: {line.strip()}"
        )


def test_the_agent_gate_explains_why_the_version_matters() -> None:
    text = (SCRIPTS / "ensure-host-prerequisites.sh").read_text(encoding="utf-8")
    assert "ENV_VAR" in text
    assert "raw string substitution" in text


# =============================================================================
# I. Toolchain lock matches the approved host
# =============================================================================


def test_the_compose_plugin_path_matches_the_live_host() -> None:
    lock = load_lock()
    path = lock["docker_compose"]["linux_aarch64"]["install_path"]
    assert path == "/usr/local/lib/docker/cli-plugins/docker-compose"


def test_no_duplicate_plugin_path_is_used() -> None:
    """Two plugins on the search path means an unrelated Docker upgrade can
    change which Compose runs, with nothing having been deployed."""
    lock = load_lock()
    assert "/usr/libexec" not in lock["docker_compose"]["linux_aarch64"]["install_path"]
    code = executable_lines(SCRIPTS / "ensure-host-prerequisites.sh")
    assert "/usr/libexec/docker/cli-plugins" not in code


def test_the_docker_log_driver_matches_the_live_host() -> None:
    expected = load_lock()["docker_daemon"]["expected"]
    assert expected["log-driver"] == "local"
    assert expected["log-opts"] == {"max-size": "50m", "max-file": "3"}
    assert expected["data-root"] == "/var/lib/radio/docker"


def test_the_compose_digest_is_unchanged() -> None:
    lock = load_lock()
    assert lock["docker_compose"]["version"] == "v5.3.1"
    assert lock["docker_compose"]["linux_aarch64"]["sha256"] == (
        "aa611e811d0ea25897839c404bfb5bf93ce706dc51c500a4457890f5d0606a86"
    )


def test_the_active_compose_plugin_is_verified_not_just_installed() -> None:
    code = executable_lines(SCRIPTS / "ensure-host-prerequisites.sh")
    assert "docker compose version --short" in code
    assert "an unexpected compose plugin is active" in code


def test_the_daemon_config_is_compared_field_by_field() -> None:
    code = executable_lines(SCRIPTS / "ensure-host-prerequisites.sh")
    assert "does not match the approved baseline" in code
    assert "unexpected TCP listener" in code, "an exposed Docker API is root on the host"


def test_the_systemd_mount_requirement_is_checked() -> None:
    """Without it Docker can start before the data volume mounts and write its
    entire image store to the root filesystem."""
    lock = load_lock()
    assert lock["docker_daemon"]["systemd_mount_requirement"] == (
        "RequiresMountsFor=/var/lib/radio/docker"
    )
    code = executable_lines(SCRIPTS / "ensure-host-prerequisites.sh")
    assert "systemctl cat docker" in code


# =============================================================================
# J. Prerequisite ordering
# =============================================================================


AUTO = SCRIPTS / "main-auto-deploy.sh"


def test_docker_is_not_required_before_the_installer_runs() -> None:
    """Requiring Docker before running the script whose job is to install
    Docker meant a bare host could never get past step one."""
    text = executable_lines(AUTO)
    first = text.index("require_commands")
    installer = text.index("ensure-host-prerequisites.sh")
    baseline = text[first:text.index("\n", first)]
    assert "docker" not in baseline, f"baseline gate must not demand docker: {baseline}"
    assert first < installer


def test_the_baseline_gate_asks_only_for_what_a_bare_host_has() -> None:
    text = AUTO.read_text(encoding="utf-8")
    first = text.index("require_commands")
    baseline = text[first:text.index("\n", first)]
    for tool in ("bash", "python3", "rpm", "dnf", "mountpoint", "flock"):
        assert tool in baseline, f"{tool} is needed to run the installer itself"


def test_the_full_toolchain_is_required_after_the_installer() -> None:
    text = executable_lines(AUTO)
    installer = text.index("ensure-host-prerequisites.sh")
    full_gate = text.index("Requiring the full toolchain")
    assert installer < full_gate
    gate_line = text[text.index("require_commands", full_gate):]
    gate_line = gate_line[:gate_line.index("\n")]
    for tool in ("git", "docker", "tar", "stat", "df", "awk", "jq", "curl", "sha256sum"):
        assert tool in gate_line, f"{tool} must be required after installation"


def test_the_ssm_document_installs_only_bootstrap_packages() -> None:
    text = cfn_yaml()
    assert "for tool in git tar; do" in text
    assert "dnf upgrade" not in text
    assert "dnf update" not in text


def test_the_bootstrap_package_set_is_recorded_in_the_lock() -> None:
    lock = load_lock()
    assert lock["packages"]["bootstrap"] == ["git", "tar"]


# =============================================================================
# K. Git runs as the repository owner
# =============================================================================


def test_the_document_runs_git_as_ec2_user() -> None:
    """This document runs as root; the clone is owned by ec2-user. Root-owned
    objects surface later as a permission error during an unrelated fetch, long
    after the cause."""
    text = cfn_text()
    assert 'runuser -u "${REPO_USER}"' in text
    assert 'env HOME="/home/${REPO_USER}"' in text


@pytest.mark.parametrize("operation", ["clone", "fetch", "cat-file", "merge-base", "archive"])
def test_every_git_operation_in_the_document_is_delegated(operation: str) -> None:
    text = cfn_text()
    document = text[text.index("DeployMainDocument:"):text.index("GitHubActionsRole:")]
    for line in document.splitlines():
        stripped = line.strip()
        if not stripped.startswith("git ") and f" git {operation}" not in stripped:
            continue
        if stripped.startswith("#"):
            continue
        assert "as_repo_user" in stripped or "runuser" in stripped, (
            f"undelegated git operation: {stripped}"
        )


def test_the_document_verifies_clone_ownership_and_symlinks() -> None:
    text = cfn_text()
    assert 'stat -c \'%U\' "${REPO_DIR}"' in text
    assert 'if [ -L "${REPO_DIR}" ]' in text
    assert 'if [ -L "${REPO_DIR}/.git" ]' in text


def test_main_auto_deploy_also_delegates_git_and_checks_ownership() -> None:
    code = executable_lines(AUTO)
    assert "repo_git()" in code
    assert "runuser -u" in code
    assert "is owned by" in code
    assert 'if [ -L "${SOURCE_REPO}" ]' in code


# =============================================================================
# L. Exact-image configuration validation
# =============================================================================


CONFIG = SCRIPTS / "ensure-production-config.sh"
DEPLOY = SCRIPTS / "deploy-compose.sh"


def test_the_host_side_check_is_stdlib_only() -> None:
    """It runs on the host's bare python, which has no pydantic -- and importing
    from the source working tree would validate whatever is checked out, not the
    commit being deployed."""
    code = executable_lines(CONFIG)
    assert "from app.config import Settings" not in code
    assert "structural checks passed" in code


def test_the_real_settings_model_runs_inside_the_exact_image() -> None:
    code = executable_lines(DEPLOY)
    assert "app.cli.validate_configuration" in code
    assert '"${RADIO_API_IMAGE}"' in code


def test_the_exact_image_validation_is_network_isolated() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    block = text[text.index("Validating configuration against the exact image"):
                 text.index("15/16 Backing up")]
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges"):
        assert flag in block, f"{flag} missing from the validation container"
    assert "-p " not in block and "--publish" not in block


def test_validation_runs_before_the_backup_and_migration() -> None:
    """A configuration that cannot load must stop the deployment before it
    changes any persistent state."""
    # Compared against the INVOCATIONS. `migrate-db.sh` is also named earlier in
    # an operator-facing warning string, which is not the migration running.
    text = executable_lines(DEPLOY)
    validation = text.index("-m app.cli.validate_configuration")
    backup = text.index('bash "${RELEASE_DIR}/scripts/backup-sqlite.sh"')
    migration = text.index('bash "${RELEASE_DIR}/scripts/migrate-db.sh"')
    assert validation < backup, "validation must precede the backup"
    assert validation < migration, "validation must precede the migration"


def test_the_validator_prints_no_secret_or_endpoint() -> None:
    from app.cli.validate_configuration import SAFE_FIELDS

    for forbidden in (
        "RADIO_AUDIO_TOKEN_SECRET", "RADIO_S3_BUCKET",
        "RADIO_SEGMENT_QUEUE_URL", "RADIO_ANALYSIS_QUEUE_URL", "AWS_REGION",
    ):
        assert forbidden not in SAFE_FIELDS, f"{forbidden} must not be reported"


def test_the_validator_reports_the_operational_facts() -> None:
    from app.cli.validate_configuration import SAFE_FIELDS

    for expected in (
        "RADIO_PIPELINE_MODE", "RADIO_QUEUE_BACKEND",
        "RADIO_SEGMENT_STORE", "RADIO_MAX_ACTIVE_UNIQUE_STATIONS",
    ):
        assert expected in SAFE_FIELDS


def test_the_validator_runs_against_the_real_model(tmp_path: Path, monkeypatch) -> None:
    import os

    from app.cli.validate_configuration import main

    monkeypatch.setenv("RADIO_S3_BUCKET", "bucket-for-tests")
    monkeypatch.setenv("RADIO_AUDIO_TOKEN_SECRET", "z" * 48)
    monkeypatch.setenv("RADIO_DATABASE_PATH", str(tmp_path / "radio.db"))
    monkeypatch.setenv("RADIO_MAX_ACTIVE_UNIQUE_STATIONS", "1")
    # The model enforces that listener sessions cannot exceed unique-station
    # capacity, which is exactly the kind of cross-field rule a structural
    # host-side check cannot express -- and why layer B exists.
    monkeypatch.setenv("RADIO_LISTENER_MAX_SESSIONS", "1")
    assert main([]) == 0
    assert os.environ["RADIO_AUDIO_TOKEN_SECRET"] == "z" * 48


def test_the_validator_fails_closed_on_an_invalid_configuration(monkeypatch) -> None:
    from app.cli.validate_configuration import main

    monkeypatch.delenv("RADIO_S3_BUCKET", raising=False)
    monkeypatch.delenv("RADIO_AUDIO_TOKEN_SECRET", raising=False)
    assert main([]) == 2


def test_a_stale_working_tree_cannot_satisfy_the_exact_image_check() -> None:
    """The whole point: the model doing the validating ships in the image built
    from the reviewed commit, so a source clone whose working tree was never
    moved cannot be what answers."""
    code = executable_lines(DEPLOY)
    validation = code[code.index("validate_configuration") - 800:code.index("validate_configuration") + 200]
    assert "${RADIO_API_IMAGE}" in validation
    assert "${REPO_DIR}" not in validation, "validation must not read the source clone"


# =============================================================================
# M. Strict VAD
# =============================================================================


MODELS = SCRIPTS / "ensure-models.sh"
VERIFY = SCRIPTS / "verify-models.py"


def test_the_verifier_offers_a_strict_mode() -> None:
    text = VERIFY.read_text(encoding="utf-8")
    assert "--require-present" in text


def test_deployment_always_verifies_strictly() -> None:
    """'Optional' means the classifier may degrade, not that a deployment may
    silently ship without a model the lock pins."""
    code = executable_lines(MODELS)
    assert code.count("--require-present") >= 2, "both verify calls must be strict"


def test_a_missing_vad_is_not_reported_as_verified_under_strict_mode() -> None:
    text = VERIFY.read_text(encoding="utf-8")
    assert "and not args.require_present" in text


def test_the_verifier_states_what_it_did_not_check_for_vad() -> None:
    """models.lock.json pins no upstream digest for the VAD file. Calling that
    'verified' without qualification would claim a check that never happened."""
    text = VERIFY.read_text(encoding="utf-8")
    assert "presence and exact size only" in text
    assert "no sha256 is pinned upstream" in text


def test_the_lock_still_pins_digests_for_asr_and_llm() -> None:
    lock = json.loads((REPO_ROOT / "models.lock.json").read_text(encoding="utf-8"))
    for name, model in lock["models"].items():
        if model["role"] not in ("asr", "llm"):
            continue
        digests = [f.get("sha256") for f in model["files"] if f.get("size_bytes")]
        assert any(digests), f"{name} must keep a pinned digest"


def test_the_permissive_mode_still_exists_for_non_deployment_callers() -> None:
    text = VERIFY.read_text(encoding="utf-8")
    assert "--allow-missing-optional" in text
    assert "optional and unavailable" in text


def test_an_existing_wrong_size_model_is_never_overwritten() -> None:
    code = executable_lines(MODELS)
    assert "refusing to overwrite or delete an existing model that does not verify" in code


# =============================================================================
# N. Main-only safety is unchanged
# =============================================================================


def test_auto_deploy_is_still_disabled_by_default() -> None:
    assert 'AUTO_DEPLOY_ENABLED:-0}" != "1"' in workflow_text()


def test_the_production_environment_is_still_used() -> None:
    assert "environment: production" in workflow_text()


def test_the_document_version_is_still_explicit() -> None:
    text = workflow_yaml()
    assert "--document-version" in text
    assert "$LATEST" not in text


def test_concurrency_still_never_cancels_a_running_deployment() -> None:
    assert "cancel-in-progress: false" in workflow_text()


def test_the_host_still_requires_the_commit_to_be_on_main() -> None:
    assert "merge-base --is-ancestor" in executable_lines(AUTO)
    assert "merge-base --is-ancestor" in cfn_text()
