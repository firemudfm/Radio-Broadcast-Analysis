"""Configuration hygiene: public templates and explicit model acquisition.

Two properties are pinned here, both of which regressed once already:

1. ``.env.example`` is a **public template**. It must not carry the production
   account id, bucket or queue URLs. An account id is not a secret, but
   publishing one is free reconnaissance, and a realistic-looking value invites
   somebody to copy it into a deployment where it half-works.

2. Models are acquired by an **explicit operator command**, never at runtime.
   Nothing in the runtime tree may download a model, and no document may claim
   a setting exists that turns downloading on.

Everything here reads tracked files or an argument parser. Nothing touches the
network and nothing writes a model.
"""
from __future__ import annotations

import ast
import re
import subprocess  # nosec B404 - fixed interpreter, argument arrays, no shell
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

ENV_EXAMPLE = REPO_ROOT / ".env.example"
LLM_ENTRYPOINT = REPO_ROOT / "docker" / "entrypoints" / "llm.sh"
DOWNLOADER = REPO_ROOT / "scripts" / "download-models.py"
VERIFIER = REPO_ROOT / "scripts" / "verify-models.py"
TRANSCRIPTION = REPO_ROOT / "app" / "services" / "transcription.py"

# Detected by SHAPE, never by literal.
#
# Writing the real account id into this assertion would put it back into a
# tracked file -- the exact thing the test exists to prevent. A 12-digit id
# inside an AWS URL, ARN or generated bucket name is the pattern that matters,
# and it catches any account, not just today's.
AWS_ACCOUNT_IN_URL = re.compile(
    r"(?:sqs|s3|lambda|execute-api)[.\-][a-z0-9.\-]*amazonaws\.com/\d{12}"
    r"|arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:"
    r"|[a-z0-9\-]+-\d{12}-[a-z0-9\-]+"
)


@pytest.fixture(scope="module")
def env_example() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def assignments(text: str) -> dict[str, str]:
    """Parse `KEY=value` lines, ignoring comments and blanks."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


# --- A/B/C. no production identifiers -----------------------------------------


def test_no_aws_account_id_in_any_form(env_example: str) -> None:
    """No 12-digit account id in a URL, ARN or generated bucket name."""
    found = AWS_ACCOUNT_IN_URL.findall(env_example)
    assert found == [], f"account-scoped identifiers in a public template: {found}"


def test_no_bare_twelve_digit_number(env_example: str) -> None:
    """Belt and braces: an account id on its own line is still an account id."""
    for line in env_example.splitlines():
        if line.strip().startswith("#"):
            continue
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", line), f"12-digit id in: {line!r}"


def test_no_production_queue_names(env_example: str) -> None:
    assert "radio-transcription.fifo" not in env_example
    assert "radio-analysis.fifo" not in env_example


def test_no_amazonaws_endpoint_at_all(env_example: str) -> None:
    """A real endpoint implies a real account; the template needs neither."""
    for line in env_example.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "amazonaws.com" not in line, f"live endpoint in: {line!r}"


def test_no_other_infrastructure_identifiers(env_example: str) -> None:
    """The instance id, Elastic IP and OIDC role ARN belong nowhere near this."""
    assert "i-0" not in env_example, "no EC2 instance id"
    assert "GitHubActionsRadioDeployRole" not in env_example
    assert "arn:aws:iam::" not in env_example


# --- D/E. safe placeholders ---------------------------------------------------


def test_bucket_placeholder_is_obviously_non_functional(env_example: str) -> None:
    assert assignments(env_example)["RADIO_S3_BUCKET"] == "replace-me-radio-bucket"


@pytest.mark.parametrize(
    "key", ["RADIO_TRANSCRIPTION_QUEUE_URL", "RADIO_ANALYSIS_QUEUE_URL"]
)
def test_queue_placeholders_are_empty(env_example: str, key: str) -> None:
    """Empty, not a dummy URL.

    A plausible-looking URL would pass start-up validation and point the
    pipeline at a queue that does not exist. Empty makes `shared_sqs` + `sqs`
    fail immediately with a named configuration error.
    """
    values = assignments(env_example)
    assert key in values, f"{key} must still be present as a documented key"
    assert values[key] == "", f"{key} must be empty, got {values[key]!r}"


def test_region_is_retained_as_a_non_secret_example(env_example: str) -> None:
    assert assignments(env_example).get("AWS_REGION") == "eu-north-1"


# --- F. no static credentials -------------------------------------------------


@pytest.mark.parametrize(
    "key", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
)
def test_no_static_credential_variables(env_example: str, key: str) -> None:
    assert key not in assignments(env_example), (
        "EC2 receives credentials through its instance role; a static key in an "
        "env file has no rotation and no audit trail"
    )


def test_the_audio_token_secret_is_still_a_placeholder(env_example: str) -> None:
    value = assignments(env_example)["RADIO_AUDIO_TOKEN_SECRET"]
    assert value.startswith("replace-me"), "must never be a real secret"


# --- G. downloader CLI --------------------------------------------------------


def run_downloader(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [sys.executable, str(DOWNLOADER), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_downloader_accepts_role_llm() -> None:
    result = run_downloader("--help")
    assert result.returncode == 0
    assert "--role" in result.stdout


def test_downloader_rejects_the_bare_llm_flag() -> None:
    """`--llm` never existed; llm.sh used to recommend it."""
    result = run_downloader("--llm")
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr or "error" in result.stderr.lower()


@pytest.mark.parametrize("role", ["asr", "llm", "vad"])
def test_downloader_accepts_every_documented_role(role: str, tmp_path: Path) -> None:
    result = run_downloader("--root", str(tmp_path), "--role", role, "--dry-run")
    assert result.returncode == 0, result.stderr


# --- H. entrypoint guidance ---------------------------------------------------


def test_llm_entrypoint_recommends_the_real_flag() -> None:
    text = LLM_ENTRYPOINT.read_text(encoding="utf-8")
    assert "--role llm" in text
    assert "--llm" not in text.replace("--role llm", ""), "the bare --llm flag does not exist"


def test_llm_entrypoint_points_at_the_verifier_too() -> None:
    text = LLM_ENTRYPOINT.read_text(encoding="utf-8")
    assert "verify-models.py" in text, "download without verify is half the workflow"


def test_llm_entrypoint_does_not_download_anything() -> None:
    text = LLM_ENTRYPOINT.read_text(encoding="utf-8")
    for forbidden in ("curl ", "wget ", "python3 -c", "pip install"):
        assert forbidden not in text, f"entrypoint must not fetch: {forbidden!r}"


# --- I. no automatic-download claim -------------------------------------------


def test_no_runtime_file_mentions_an_automatic_download_switch() -> None:
    """The setting never existed; the runtime tree must not imply otherwise."""
    roots = [
        REPO_ROOT / "app",
        REPO_ROOT / "docker",
        REPO_ROOT / "scripts",
        ENV_EXAMPLE,
    ]
    offenders = []
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in files:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".sh", ".yaml", ".yml", ".example", ""}:
                continue
            try:
                if "ALLOW_MODEL_DOWNLOAD" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
            except (UnicodeDecodeError, OSError):
                continue
    assert offenders == [], f"stale automatic-download references: {offenders}"


def test_no_adr_claims_the_switch_functions() -> None:
    adr = REPO_ROOT / "docs" / "architecture" / "adr"
    for path in sorted(adr.glob("*.md")):
        assert "ALLOW_MODEL_DOWNLOAD" not in path.read_text(encoding="utf-8"), path.name


def test_model_management_only_mentions_it_as_removed() -> None:
    """One historical note is allowed, so a reader of the old docs finds it."""
    text = (REPO_ROOT / "docs" / "MODEL_MANAGEMENT.md").read_text(encoding="utf-8")
    mentions = [line for line in text.splitlines() if "ALLOW_MODEL_DOWNLOAD" in line]
    assert len(mentions) <= 1, "should survive only as the historical note"
    if mentions:
        note = text[text.index(mentions[0]) : text.index(mentions[0]) + 400]
        assert "never existed" in note or "removed" in note


# --- J. missing ASR model is a permanent local error --------------------------


def test_transcription_engine_has_no_download_capability() -> None:
    """Structural: no downloader, no allow_download, no repo-id load path."""
    source = TRANSCRIPTION.read_text(encoding="utf-8")
    assert "allow_download" not in source
    for forbidden in ("huggingface_hub", "snapshot_download", "hf_hub_download", "requests."):
        assert forbidden not in source, f"engine must not import a downloader: {forbidden}"

    tree = ast.parse(source)
    engine = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "FasterWhisperEngine"
    )
    init = next(
        node
        for node in engine.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    names = {arg.arg for arg in init.args.args} | {arg.arg for arg in init.args.kwonlyargs}
    assert "allow_download" not in names, "the dead escape hatch must stay removed"


def test_a_missing_model_raises_a_permanent_error_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake faster_whisper proves no remote id is ever passed to the loader."""
    import types

    from app.pipeline.errors import ModelVerificationError
    from app.services.transcription import DecodeOptions, FasterWhisperEngine

    loaded: list[str] = []

    module = types.ModuleType("faster_whisper")

    class _WhisperModel:
        def __init__(self, source, **_kwargs):
            loaded.append(str(source))

    module.WhisperModel = _WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    engine = FasterWhisperEngine(
        model_name="Systran/faster-whisper-small", model_root=tmp_path / "models"
    )
    with pytest.raises(ModelVerificationError) as error:
        engine.transcribe(b"\x00\x00" * 100, DecodeOptions())

    assert not error.value.retryable, "a missing model will not appear on retry"
    assert loaded == [], "the loader must never be reached without a local model"
    detail = error.value.detail or ""
    assert "download-models.py" in detail and "--role asr" in detail
    assert "verify-models.py" in detail


def test_a_present_model_directory_loads_from_the_local_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complement: a local directory is used, and a repo id never is."""
    import types

    from app.services.transcription import DecodeOptions, FasterWhisperEngine

    loaded: list[str] = []

    module = types.ModuleType("faster_whisper")

    class _WhisperModel:
        def __init__(self, source, **_kwargs):
            loaded.append(str(source))

        def transcribe(self, *_args, **_kwargs):
            class _Info:
                language = "en"
                language_probability = 0.9
                duration = 1.0

            return [], _Info()

    module.WhisperModel = _WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    root = tmp_path / "models"
    (root / "asr" / "Systran__faster-whisper-small").mkdir(parents=True)
    engine = FasterWhisperEngine(
        model_name="Systran/faster-whisper-small", model_root=root
    )
    engine.transcribe(b"\x00\x00" * 100, DecodeOptions())

    assert len(loaded) == 1
    assert loaded[0].endswith("Systran__faster-whisper-small")
    assert "Systran/faster-whisper-small" not in loaded[0], "never a bare repo id"


# --- K. downloader stays explicit ---------------------------------------------


def test_dry_run_writes_no_model_file(tmp_path: Path) -> None:
    result = run_downloader("--root", str(tmp_path), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "would fetch" in result.stdout
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written == [], f"dry run must not create files, found {written}"


def test_verifier_reports_a_missing_model_rather_than_fetching(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, argument array, no shell
        [sys.executable, str(VERIFIER), "--root", str(tmp_path), "--role", "asr"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 1
    assert "missing" in (result.stdout + result.stderr).lower()
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
