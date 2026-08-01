"""Keyword evidence must survive the analysis queue (Phase 4/5 contract).

``MatchedKeywordRef`` is the only channel by which match metadata reaches the
analysis worker: that worker never sees the audio, the per-segment transcript,
or the station's keyword index. Anything the contract drops is not "defaulted"
downstream, it is fabricated -- and the result writer persists it into
``mention_keywords`` permanently.

These tests pin both halves of the contract: the new fields carry real values,
and a ``radio.analysis.v1`` message serialised before those fields existed
still parses with documented fallbacks.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.pipeline.contracts import (
    ANALYSIS_SCHEMA_V1,
    MAX_MESSAGE_BYTES,
    AnalysisJobV1,
    MatchedKeywordRef,
    TranscriptReference,
    parse_analysis_job,
)
from app.pipeline.errors import InvalidMessageError, MessageTooLargeError

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)

JOB_ID = "11111111-1111-4111-8111-111111111111"
MENTION_ID = "22222222-2222-4222-8222-222222222222"
CONVERSATION_ID = "33333333-3333-4333-8333-333333333333"
TRACE_ID = "44444444-4444-4444-8444-444444444444"
TRANSCRIPT_ID = "55555555-5555-4555-8555-555555555555"
KEYWORD_ID = "66666666-6666-4666-8666-666666666666"
CAMPAIGN_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CAMPAIGN_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def keyword_ref(**overrides) -> MatchedKeywordRef:
    payload = {
        "keyword_id": KEYWORD_ID,
        "campaign_ids": [CAMPAIGN_A],
        "canonical_value": "NVIDIA",
        "matched_text": "एनवीडिया",
        "match_level": "alias",
        "start_char": 44,
        "end_char": 52,
        "start_ms": 21_000,
        "end_ms": 22_500,
        "confidence": 0.95,
    }
    payload.update(overrides)
    return MatchedKeywordRef(**payload)


def analysis_job(refs: list[MatchedKeywordRef]) -> AnalysisJobV1:
    return AnalysisJobV1(
        analysis_job_id=JOB_ID,
        mention_id=MENTION_ID,
        conversation_id=CONVERSATION_ID,
        station_id="rb-station",
        language="hi",
        transcript_reference=TranscriptReference(transcript_id=TRANSCRIPT_ID),
        matched_keywords=refs,
        campaign_ids=[CAMPAIGN_A, CAMPAIGN_B],
        trace_id=TRACE_ID,
        created_at=NOW,
    )


# --- A. backwards compatibility -----------------------------------------------


LEGACY_BODY = json.dumps(
    {
        "schema": ANALYSIS_SCHEMA_V1,
        "analysis_job_id": JOB_ID,
        "mention_id": MENTION_ID,
        "conversation_id": CONVERSATION_ID,
        "station_id": "rb-station",
        "language": "hi",
        "transcript_reference": {"backend": "sqlite", "transcript_id": TRANSCRIPT_ID},
        # Exactly the five fields the pre-change producer emitted.
        "matched_keywords": [
            {
                "keyword_id": KEYWORD_ID,
                "canonical_value": "NVIDIA",
                "matched_text": "एनवीडिया",
                "start_ms": 21_000,
                "end_ms": 22_500,
            }
        ],
        "campaign_ids": [CAMPAIGN_A, CAMPAIGN_B],
        "trace_id": TRACE_ID,
        "created_at": "2026-07-27T12:00:00Z",
    }
)


def test_a_message_queued_before_this_change_still_parses() -> None:
    """A message already sitting in SQS must not become a poison message."""
    job = parse_analysis_job(LEGACY_BODY)
    assert job.schema_name == ANALYSIS_SCHEMA_V1, "no v2 contract for additive fields"

    ref = job.matched_keywords[0]
    assert ref.match_level == "exact", "documented legacy fallback"
    assert ref.confidence == 1.0
    assert ref.start_char == 0
    assert ref.end_char is None
    assert ref.campaign_ids == []


def test_legacy_fallbacks_resolve_the_way_the_old_code_behaved() -> None:
    job = parse_analysis_job(LEGACY_BODY)
    ref = job.matched_keywords[0]

    # end_char is derived from the matched text only when it was absent.
    assert ref.resolved_end_char == len("एनवीडिया")
    # Ownership falls back to the job-level list, which is all the old message
    # ever carried.
    assert ref.resolved_campaign_ids(job.campaign_ids) == (CAMPAIGN_A, CAMPAIGN_B)


def test_a_new_message_does_not_use_any_fallback() -> None:
    job = parse_analysis_job(analysis_job([keyword_ref()]).to_body())
    ref = job.matched_keywords[0]

    assert ref.match_level == "alias"
    assert ref.confidence == pytest.approx(0.95)
    assert ref.resolved_end_char == 52
    assert ref.resolved_campaign_ids(job.campaign_ids) == (CAMPAIGN_A,)


def test_the_schema_string_is_unchanged() -> None:
    assert analysis_job([keyword_ref()]).schema_name == "radio.analysis.v1"


# --- B. validation ------------------------------------------------------------


@pytest.mark.parametrize("value", [-0.01, 1.01, 5.0])
def test_confidence_outside_zero_to_one_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        keyword_ref(confidence=value)


def test_end_char_before_start_char_is_rejected() -> None:
    with pytest.raises(ValidationError, match="end_char must not precede start_char"):
        keyword_ref(start_char=50, end_char=10)


def test_end_ms_before_start_ms_is_still_rejected() -> None:
    with pytest.raises(ValidationError, match="end_ms must not precede start_ms"):
        keyword_ref(start_ms=5_000, end_ms=1_000)


def test_negative_offsets_are_rejected() -> None:
    with pytest.raises(ValidationError):
        keyword_ref(start_char=-1)
    with pytest.raises(ValidationError):
        keyword_ref(end_char=-1)


def test_an_invalid_campaign_uuid_is_rejected() -> None:
    with pytest.raises(ValidationError):
        keyword_ref(campaign_ids=["not-a-uuid"])


def test_duplicate_campaign_ids_are_normalised_preserving_order() -> None:
    ref = keyword_ref(campaign_ids=[CAMPAIGN_B, CAMPAIGN_A, CAMPAIGN_B])
    assert ref.campaign_ids == [CAMPAIGN_B, CAMPAIGN_A]


def test_unknown_fields_remain_forbidden() -> None:
    with pytest.raises(ValidationError):
        keyword_ref(surprise="value")


def test_the_record_stays_frozen() -> None:
    ref = keyword_ref()
    with pytest.raises(ValidationError):
        ref.match_level = "fuzzy"  # type: ignore[misc]


def test_an_oversized_message_still_fails_loudly() -> None:
    """Ownership must never be dropped silently to fit the size ceiling.

    Per-match ownership is what makes a message able to grow: 50 keywords each
    tracked by many campaigns. The producer must hit the named
    MessageTooLargeError rather than quietly shedding campaign ids.
    """
    many_campaigns = [f"{index:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa" for index in range(100)]
    refs = [
        keyword_ref(
            keyword_id=f"{index:08x}-6666-4666-8666-666666666666",
            campaign_ids=many_campaigns,
            canonical_value="B" * 200,
            matched_text="M" * 300,
        )
        for index in range(50)
    ]
    with pytest.raises(MessageTooLargeError):
        analysis_job(refs).to_body()


def test_a_realistic_message_stays_well_under_the_ceiling() -> None:
    refs = [
        keyword_ref(keyword_id=f"{index:08x}-6666-4666-8666-666666666666")
        for index in range(10)
    ]
    body = analysis_job(refs).to_body()
    assert len(body.encode("utf-8")) < MAX_MESSAGE_BYTES


def test_a_malformed_body_is_still_a_permanent_error() -> None:
    with pytest.raises(InvalidMessageError):
        parse_analysis_job('{"schema": "radio.analysis.v1"}')


# --- round trip ---------------------------------------------------------------


def test_every_field_survives_serialisation() -> None:
    original = keyword_ref()
    restored = parse_analysis_job(analysis_job([original]).to_body()).matched_keywords[0]
    assert restored == original, "the wire round trip must be lossless"


def test_per_match_ownership_is_narrower_than_the_job(
) -> None:
    """The two campaign lists mean different things and must stay distinct."""
    job = analysis_job([keyword_ref(campaign_ids=[CAMPAIGN_A])])
    restored = parse_analysis_job(job.to_body())

    assert restored.campaign_ids == [CAMPAIGN_A, CAMPAIGN_B], "conversation-level"
    assert restored.matched_keywords[0].campaign_ids == [CAMPAIGN_A], "keyword-level"
