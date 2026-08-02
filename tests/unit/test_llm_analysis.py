"""LLM analysis: schema validation, evidence verification, breaker, fallback."""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services.llm_analysis import (
    RESPONSE_JSON_SCHEMA,
    AnalysisRequest,
    AnalysisResult,
    CircuitBreaker,
    ConversationAnalyzer,
    FakeLlmClient,
)

TRANSCRIPT = (
    "Welcome back. Buy the new NVIDIA laptop today at great prices, "
    "available at all stores nationwide."
)


@pytest.fixture
def settings() -> Settings:
    return Settings(RADIO_S3_BUCKET="b", RADIO_AUDIO_TOKEN_SECRET="x" * 48)


@pytest.fixture
def request_() -> AnalysisRequest:
    return AnalysisRequest(
        conversation_id="conversation-1",
        transcript=TRANSCRIPT,
        language="en",
        content_type="advertisement",
        duration_ms=20_000,
        matched_keywords=("NVIDIA",),
        station_name="Test FM",
    )


def good_response(**overrides) -> str:
    document = {
        "content_type": "advertisement",
        "language": "en",
        "relevant": True,
        "summary": "An advertisement for an NVIDIA laptop.",
        "translated_summary": "An advertisement for an NVIDIA laptop.",
        "main_topic": "NVIDIA laptop promotion",
        "sentiment": "positive",
        "speaker_stance": "promotional",
        "urgency": "normal",
        "entities": [{"name": "NVIDIA", "type": "organization"}],
        "key_points": ["Laptop is on promotion"],
        "evidence": [{"text": "the new NVIDIA laptop", "start_ms": 1000, "end_ms": 3000}],
        "confidence": 0.91,
    }
    document.update(overrides)
    return json.dumps(document)


def analyzer(settings: Settings, *responses: str, **kwargs) -> ConversationAnalyzer:
    return ConversationAnalyzer(
        settings, client=FakeLlmClient(responses=list(responses)), **kwargs
    )


# --- happy path ---------------------------------------------------------------


def test_a_valid_response_is_accepted(settings: Settings, request_: AnalysisRequest) -> None:
    result = analyzer(settings, good_response()).analyze(request_)
    assert result.status == "ready"
    assert result.sentiment == "positive"
    assert result.speaker_stance == "promotional"
    assert result.confidence == pytest.approx(0.91)
    assert [entity.name for entity in result.entities] == ["NVIDIA"]
    assert result.model == "fake-qwen"


def test_exactly_one_call_per_conversation(
    settings: Settings, request_: AnalysisRequest
) -> None:
    client = FakeLlmClient(responses=[good_response()])
    ConversationAnalyzer(settings, client=client).analyze(request_)
    assert len(client.calls) == 1, "the LLM runs once per conversation, not per segment"


def test_non_thinking_mode_is_requested(settings: Settings, request_: AnalysisRequest) -> None:
    schema_seen = {}

    class RecordingClient(FakeLlmClient):
        def complete(self, messages, *, schema=None):
            schema_seen["schema"] = schema
            return good_response()

    ConversationAnalyzer(settings, client=RecordingClient()).analyze(request_)
    assert schema_seen["schema"] is RESPONSE_JSON_SCHEMA


# --- evidence verification ----------------------------------------------------


def test_evidence_not_present_in_the_transcript_is_dropped(
    settings: Settings, request_: AnalysisRequest
) -> None:
    """A quote attributed to a station that was never broadcast is the worst
    output this system could produce."""
    response = good_response(
        evidence=[
            {"text": "the new NVIDIA laptop", "start_ms": 1000, "end_ms": 3000},
            {"text": "we are going out of business", "start_ms": 0, "end_ms": 500},
        ]
    )
    result = analyzer(settings, response).analyze(request_)
    assert [item.text for item in result.evidence] == ["the new NVIDIA laptop"]
    assert result.needs_review, "dropping a quote must be visible, not silent"


def test_evidence_timestamps_outside_the_conversation_are_clamped_or_dropped(
    settings: Settings, request_: AnalysisRequest
) -> None:
    response = good_response(
        evidence=[{"text": "the new NVIDIA laptop", "start_ms": 999_000, "end_ms": 999_500}]
    )
    result = analyzer(settings, response).analyze(request_)
    assert result.evidence == [], "a timestamp past the end would cut the wrong audio"


def test_evidence_end_is_clamped_to_the_conversation(
    settings: Settings, request_: AnalysisRequest
) -> None:
    response = good_response(
        evidence=[{"text": "the new NVIDIA laptop", "start_ms": 1000, "end_ms": 999_000}]
    )
    result = analyzer(settings, response).analyze(request_)
    assert result.evidence[0].end_ms == request_.duration_ms


# --- malformed output ---------------------------------------------------------


def test_invalid_json_gets_a_bounded_repair_retry(
    settings: Settings, request_: AnalysisRequest
) -> None:
    client = FakeLlmClient(responses=["this is not json", good_response()])
    result = ConversationAnalyzer(settings, client=client).analyze(request_)
    assert result.status == "ready"
    assert len(client.calls) == 2
    assert "not valid for the required schema" in client.calls[1][1]["content"]


def test_repeated_invalid_json_falls_back_deterministically(
    settings: Settings, request_: AnalysisRequest
) -> None:
    result = analyzer(settings, "garbage", "still garbage").analyze(request_)
    assert result.status == "fallback"
    assert result.needs_review
    assert result.confidence == 0.0
    # The mention itself is not lost: the transcript is still the record.
    assert "NVIDIA" in result.summary
    assert [entity.name for entity in result.entities] == ["NVIDIA"]


def test_a_reasoning_block_is_stripped_and_never_stored(
    settings: Settings, request_: AnalysisRequest
) -> None:
    response = "<think>Let me consider the options carefully.</think>" + good_response()
    result = analyzer(settings, response).analyze(request_)
    assert result.status == "ready"
    serialised = json.dumps(result.as_payload())
    assert "Let me consider" not in serialised
    assert "<think>" not in serialised


def test_json_wrapped_in_a_code_fence_is_recovered(
    settings: Settings, request_: AnalysisRequest
) -> None:
    result = analyzer(settings, f"```json\n{good_response()}\n```").analyze(request_)
    assert result.status == "ready"


def test_unknown_enum_values_are_coerced_not_rejected(
    settings: Settings, request_: AnalysisRequest
) -> None:
    result = analyzer(settings, good_response(sentiment="ecstatic", urgency="whenever")).analyze(
        request_
    )
    assert result.sentiment == "neutral"
    assert result.urgency == "normal"


def test_out_of_range_confidence_is_rejected_and_repaired(
    settings: Settings, request_: AnalysisRequest
) -> None:
    client = FakeLlmClient(responses=[good_response(confidence=7.5), good_response()])
    result = ConversationAnalyzer(settings, client=client).analyze(request_)
    assert result.status == "ready"
    assert 0.0 <= result.confidence <= 1.0


def test_unexpected_fields_are_rejected(settings: Settings, request_: AnalysisRequest) -> None:
    result = analyzer(settings, good_response(surprise="value"), "junk").analyze(request_)
    assert result.status == "fallback"


# --- resilience ---------------------------------------------------------------


def test_a_timeout_falls_back_rather_than_failing_the_job(
    settings: Settings, request_: AnalysisRequest
) -> None:
    client = FakeLlmClient(failure=TimeoutError("model is wedged"))
    result = ConversationAnalyzer(settings, client=client).analyze(request_)
    assert result.status == "fallback"
    assert "NVIDIA" in result.summary


def test_the_circuit_breaker_opens_and_short_circuits(
    settings: Settings, request_: AnalysisRequest
) -> None:
    client = FakeLlmClient(failure=TimeoutError("down"))
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60, clock=lambda: 0.0)
    subject = ConversationAnalyzer(settings, client=client, breaker=breaker)

    subject.analyze(request_)
    assert breaker.is_open, "repeated failures must stop hammering the model"

    calls_before = len(client.calls)
    result = subject.analyze(request_)
    assert result.status == "fallback"
    assert len(client.calls) == calls_before, "an open circuit must not call the model"


def test_the_circuit_breaker_recovers_after_the_reset_window(settings: Settings) -> None:
    now = {"value": 0.0}
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30, clock=lambda: now["value"])
    breaker.record_failure()
    assert breaker.is_open
    now["value"] = 31.0
    assert not breaker.is_open, "the breaker must half-open, not latch permanently"


def test_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count == 0
    assert not breaker.is_open


def test_disabled_llm_returns_a_usable_record(request_: AnalysisRequest) -> None:
    settings = Settings(
        RADIO_S3_BUCKET="b", RADIO_AUDIO_TOKEN_SECRET="x" * 48, RADIO_LLM_ENABLED=False
    )
    client = FakeLlmClient(responses=[good_response()])
    result = ConversationAnalyzer(settings, client=client).analyze(request_)
    assert result.status == "disabled"
    assert client.calls == [], "a disabled LLM must not be called"
    assert result.summary


def test_the_transcript_is_truncated_to_the_input_budget(settings: Settings) -> None:
    client = FakeLlmClient(responses=[good_response()])
    long_request = AnalysisRequest(
        conversation_id="c",
        transcript="word " * 50_000,
        language="en",
        content_type="discussion",
        duration_ms=600_000,
    )
    ConversationAnalyzer(settings, client=client).analyze(long_request)
    sent = client.calls[0][1]["content"]
    assert len(sent) < len(long_request.transcript)


# --- the response grammar must actually parse ---------------------------------
#
# llama.cpp compiles maxLength into a bounded grammar repetition (char{0,2000})
# and its parser refuses large ones: "number of rules that are going to be
# repeated multiplied by the new repetition exceeds sane defaults". That failed
# EVERY analysis call in production -- the model never ran, and every mention
# carried the deterministic fallback. Length limits belong in the pydantic
# model, which truncates after decoding.


def _walk(schema, path="$"):
    if isinstance(schema, dict):
        for key, value in schema.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            yield from _walk(item, f"{path}[{index}]")


def test_the_response_schema_contains_no_string_length_bounds() -> None:
    offenders = [
        f"{path}.{key}" for path, key, _ in _walk(RESPONSE_JSON_SCHEMA)
        if key in {"maxLength", "minLength", "pattern"}
    ]
    assert not offenders, (
        f"these compile into grammar repetitions llama.cpp refuses: {offenders}"
    )


def test_array_bounds_stay_small_enough_to_compile() -> None:
    """maxItems is kept -- it caps how many objects are generated, which
    truncation cannot do afterwards -- but only because the bounds are tiny.
    A {0,19} repetition parses; a {0,2000} does not."""
    for path, key, value in _walk(RESPONSE_JSON_SCHEMA):
        if key == "maxItems":
            assert isinstance(value, int) and value <= 32, f"{path}.maxItems={value}"


def test_an_overlong_summary_is_truncated_not_rejected() -> None:
    """Without the grammar bound a rambling model must degrade to a shortened
    summary, never to a failed validation and the no-model fallback."""
    result = AnalysisResult.model_validate({"summary": "x" * 5000})
    assert len(result.summary) == 2000


def test_overlong_optional_strings_are_truncated_not_rejected() -> None:
    result = AnalysisResult.model_validate(
        {"summary": "ok", "translated_summary": "y" * 5000, "main_topic": "z" * 5000}
    )
    assert len(result.translated_summary) == 2000
    assert len(result.main_topic) == 300


def test_an_overlong_entity_name_is_truncated_not_rejected() -> None:
    result = AnalysisResult.model_validate(
        {"summary": "ok", "entities": [{"name": "n" * 900}]}
    )
    assert len(result.entities[0].name) == 200


def test_an_overlong_evidence_quote_is_truncated_not_rejected() -> None:
    # A truncated quote may stop matching the transcript verbatim; the evidence
    # verifier then drops it and flags for review, which is the right outcome.
    result = AnalysisResult.model_validate(
        {"summary": "ok", "evidence": [{"text": "q" * 3000, "start_ms": 0, "end_ms": 1}]}
    )
    assert len(result.evidence[0].text) == 1000
