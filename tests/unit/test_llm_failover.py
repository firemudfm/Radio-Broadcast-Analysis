"""Chained analysis LLM tiers: hosted providers in priority order, local last.

The operator's contract: NVIDIA first; on any error Groq handles it; then
Mistral; then the local model on the box. A failed tier rests for the retry
window and is climbed back to automatically once the window expires. Analysis
never waits on a broken tier, and one dead provider never hides a healthy one.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.llm_analysis import (
    FailoverLlmClient,
    LlamaServerClient,
    build_llm_client,
)


class ScriptedClient:
    def __init__(self, name: str, *, failures: int = 0) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.failures = failures
        self.calls = 0

    def health(self) -> bool:
        return True

    def complete(self, messages, *, schema=None):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError(f"{self.name} is down")
        return f"answer from {self.name}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


RETRY = 7200.0


def make_chain(clock: FakeClock, *remote_failures: int):
    names = ["nvidia", "groq", "mistral"][: len(remote_failures)]
    remotes = [
        ScriptedClient(name, failures=failures)
        for name, failures in zip(names, remote_failures, strict=True)
    ]
    local = ScriptedClient("local")
    client = FailoverLlmClient(remotes, local, retry_seconds=RETRY, clock=clock)
    return client, remotes, local


def test_the_best_tier_serves_when_healthy() -> None:
    client, remotes, local = make_chain(FakeClock(), 0, 0, 0)
    assert client.complete([]) == "answer from nvidia"
    assert client.model == "nvidia-model"
    assert [tier.calls for tier in remotes] == [1, 0, 0]
    assert local.calls == 0


def test_a_failure_cascades_to_the_next_tier_within_the_same_call() -> None:
    client, remotes, _ = make_chain(FakeClock(), 1, 0, 0)
    assert client.complete([]) == "answer from groq"
    assert client.model == "groq-model"
    assert [tier.calls for tier in remotes] == [1, 1, 0]


def test_all_remotes_down_lands_on_the_local_model() -> None:
    client, remotes, local = make_chain(FakeClock(), 1, 1, 1)
    assert client.complete([]) == "answer from local"
    assert client.model == "local-model"
    assert [tier.calls for tier in remotes] == [1, 1, 1]
    assert local.calls == 1


def test_cooling_tiers_are_skipped_without_being_probed() -> None:
    clock = FakeClock()
    client, remotes, _ = make_chain(clock, 1, 0, 0)
    client.complete([])
    clock.now += RETRY / 2
    assert client.complete([]) == "answer from groq"
    assert remotes[0].calls == 1, "a cooling tier must not be probed"


def test_the_chain_climbs_back_after_the_window() -> None:
    clock = FakeClock()
    client, remotes, _ = make_chain(clock, 1, 0, 0)
    client.complete([])                     # nvidia fails, groq serves
    clock.now += RETRY + 1
    assert client.complete([]) == "answer from nvidia"
    assert client.model == "nvidia-model"
    assert remotes[0].calls == 2


def test_a_second_failure_rearms_that_tier_only() -> None:
    clock = FakeClock()
    client, remotes, _ = make_chain(clock, 2, 0, 0)
    client.complete([])                     # nvidia fails (1), groq serves
    clock.now += RETRY + 1
    client.complete([])                     # nvidia fails (2), groq serves again
    clock.now += RETRY / 2
    assert client.complete([]) == "answer from groq"
    assert remotes[0].calls == 2, "the re-armed cooldown must hold"
    assert remotes[1].calls == 3


def test_unusable_content_cascades_like_an_error() -> None:
    """The production incident: tier 1 answered HTTP 200 with reasoning prose
    on every call. A 200 full of garbage must walk the chain and rest the
    tier, exactly like a transport error."""

    class ProseClient(ScriptedClient):
        def complete(self, messages, *, schema=None):
            self.calls += 1
            return "I am thinking about the transcript rather than answering."

    clock = FakeClock()
    prose = ProseClient("nvidia")
    good = ScriptedClient("groq")
    local = ScriptedClient("local")
    client = FailoverLlmClient(
        [prose, good],
        local,
        retry_seconds=RETRY,
        clock=clock,
        content_check=lambda content: content.strip().startswith("{"),
    )
    good.complete = lambda messages, schema=None: '{"summary": "fine"}'  # type: ignore[method-assign]
    assert client.complete([]) == '{"summary": "fine"}'
    assert prose.calls == 1
    clock.now += RETRY / 2
    client.complete([])
    assert prose.calls == 1, "a garbage tier must rest like a failed one"


def test_cooldowns_are_independent_per_tier() -> None:
    clock = FakeClock()
    client, remotes, local = make_chain(clock, 1, 1, 0)
    client.complete([])                     # nvidia and groq fail, mistral serves
    clock.now += RETRY / 2
    assert client.complete([]) == "answer from mistral"
    assert remotes[0].calls == 1 and remotes[1].calls == 1
    assert local.calls == 0


def settings_with(tmp_path, **overrides) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="bucket",
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        **overrides,
    )


def test_remote_requests_ask_for_json_and_use_the_remote_budget(
    tmp_path, monkeypatch
) -> None:
    """Both production lessons in one contract: hosted reasoning models need
    the larger remote output budget (480 truncated mid-think), and json_object
    mode asks the provider for parseable output up front."""
    import json as jsonlib

    from app.services.llm_analysis import RemoteApiClient, RemoteTier

    settings = settings_with(tmp_path)
    tier = RemoteTier(
        name="NVIDIA",
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="key",
        timeout_seconds=5,
    )
    captured: dict = {}

    class FakeResponse:
        status = 200

        def read(self):
            return jsonlib.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["body"] = jsonlib.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr("app.services.llm_analysis.urllib.request.urlopen", fake_urlopen)
    client = RemoteApiClient(settings, tier)
    assert client.complete([{"role": "user", "content": "x"}]) == "{}"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["max_tokens"] == settings.RADIO_LLM_REMOTE_MAX_OUTPUT_TOKENS

    # Operator extra-body knobs merge in and win over the defaults.
    knobbed = RemoteTier(
        name="NVIDIA",
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="key",
        timeout_seconds=5,
        extra_body='{"reasoning_effort": "low", "max_tokens": 4096}',
    )
    RemoteApiClient(settings, knobbed).complete([{"role": "user", "content": "x"}])
    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["max_tokens"] == 4096


def test_a_malformed_extra_body_is_refused_at_startup(tmp_path) -> None:
    with pytest.raises(ValueError):
        settings_with(tmp_path, RADIO_LLM_REMOTE_EXTRA_BODY="not json")
    with pytest.raises(ValueError, match="JSON object"):
        settings_with(tmp_path, RADIO_LLM_REMOTE_EXTRA_BODY='["a", "list"]')


def test_builder_returns_local_only_when_nothing_is_enabled(settings) -> None:
    assert isinstance(build_llm_client(settings), LlamaServerClient)


def test_builder_stacks_enabled_tiers_in_priority_order(tmp_path) -> None:
    settings = settings_with(
        tmp_path,
        RADIO_LLM_REMOTE_ENABLED=True,
        RADIO_LLM_REMOTE_API_KEY="nvapi-test",
        RADIO_LLM_GROQ_ENABLED=True,
        RADIO_LLM_GROQ_API_KEY="gsk-test",
        RADIO_LLM_MISTRAL_ENABLED=True,
        RADIO_LLM_MISTRAL_API_KEY="mistral-test",
        RADIO_LLM_GEMINI_ENABLED=True,
        RADIO_LLM_GEMINI_API_KEY="gemini-test",
    )
    client = build_llm_client(settings)
    assert isinstance(client, FailoverLlmClient)
    assert client.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    tier_names = [remote.name for remote in client._remotes]  # noqa: SLF001
    assert tier_names == ["NVIDIA", "Groq", "Mistral", "Gemini"]


def test_a_partial_chain_skips_disabled_providers(tmp_path) -> None:
    settings = settings_with(
        tmp_path,
        RADIO_LLM_GROQ_ENABLED=True,
        RADIO_LLM_GROQ_API_KEY="gsk-test",
    )
    client = build_llm_client(settings)
    assert isinstance(client, FailoverLlmClient)
    assert client.model == "qwen/qwen3.6-27b"


@pytest.mark.parametrize(
    ("flag", "match"),
    [
        ({"RADIO_LLM_REMOTE_ENABLED": True}, "NVIDIA_API_KEY"),
        ({"RADIO_LLM_GROQ_ENABLED": True}, "GROQ_API_KEY"),
        ({"RADIO_LLM_MISTRAL_ENABLED": True}, "MISTRAL_API_KEY"),
        ({"RADIO_LLM_GEMINI_ENABLED": True}, "GEMINI_API_KEY"),
    ],
)
def test_enabling_a_provider_without_its_key_is_refused(tmp_path, flag, match) -> None:
    with pytest.raises(ValueError, match=match):
        settings_with(tmp_path, **flag)
