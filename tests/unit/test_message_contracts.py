"""SQS message contract validation (ADR-003)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.pipeline.contracts import (
    ANALYSIS_SCHEMA_V1,
    MAX_MESSAGE_BYTES,
    TRANSCRIPTION_SCHEMA_V1,
    AnalysisJobV1,
    MatchedKeywordRef,
    StorageDescriptor,
    TranscriptionJobV1,
    TranscriptReference,
    parse_analysis_job,
    parse_transcription_job,
)
from app.pipeline.errors import InvalidMessageError, MessageTooLargeError, UnsupportedSchemaError

DIGEST = "a" * 64
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def _storage(**overrides) -> StorageDescriptor:
    payload = {
        "backend": "local",
        "path": "/var/lib/radio/spool/rb-x/11111111-1111-4111-8111-111111111111.opus",
        "bucket": None,
        "key": None,
        "sha256": DIGEST,
        "size_bytes": 4096,
    }
    payload.update(overrides)
    return StorageDescriptor(**payload)


def _transcription(**overrides) -> TranscriptionJobV1:
    payload = {
        "schema": TRANSCRIPTION_SCHEMA_V1,
        "job_id": "11111111-1111-4111-8111-111111111111",
        "segment_id": "22222222-2222-4222-8222-222222222222",
        "station_id": "rb-abc123",
        "station_session_id": "33333333-3333-4333-8333-333333333333",
        "sequence_number": 7,
        "started_at": NOW,
        "duration_ms": 20_000,
        "content_class": "speech_over_music",
        "language_hints": ["hi", "en"],
        "keyword_index_version": 42,
        "storage": _storage(),
        "trace_id": "44444444-4444-4444-8444-444444444444",
        "created_at": NOW,
    }
    payload.update(overrides)
    return TranscriptionJobV1.model_validate(payload)


def _analysis(**overrides) -> AnalysisJobV1:
    payload = {
        "schema": ANALYSIS_SCHEMA_V1,
        "analysis_job_id": "55555555-5555-4555-8555-555555555555",
        "mention_id": "66666666-6666-4666-8666-666666666666",
        "conversation_id": "77777777-7777-4777-8777-777777777777",
        "station_id": "rb-abc123",
        "language": "hi",
        "transcript_reference": TranscriptReference(
            transcript_id="88888888-8888-4888-8888-888888888888"
        ),
        "matched_keywords": [
            MatchedKeywordRef(
                keyword_id="99999999-9999-4999-8999-999999999999",
                canonical_value="NVIDIA",
                matched_text="एनवीडिया",
                start_ms=32_000,
                end_ms=33_300,
            )
        ],
        "campaign_ids": ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
        "trace_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "created_at": NOW,
    }
    payload.update(overrides)
    return AnalysisJobV1.model_validate(payload)


# --- happy path ---------------------------------------------------------------


def test_transcription_round_trips() -> None:
    parsed = parse_transcription_job(_transcription().to_body())
    assert parsed.station_id == "rb-abc123"
    assert parsed.storage.sha256 == DIGEST
    assert parsed.language_hints == ["hi", "en"]


def test_analysis_round_trips() -> None:
    parsed = parse_analysis_job(_analysis().to_body())
    assert parsed.matched_keywords[0].matched_text == "एनवीडिया"
    assert parsed.campaign_ids == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]


def test_group_and_deduplication_ids_match_the_adr() -> None:
    """Both are load-bearing: group=station orders, dedup=id de-duplicates."""
    job = _transcription()
    assert job.message_group_id() == job.station_id
    assert job.deduplication_id() == job.segment_id

    analysis = _analysis()
    assert analysis.message_group_id() == analysis.station_id
    assert analysis.deduplication_id() == analysis.analysis_job_id


def test_serialised_body_uses_the_schema_alias() -> None:
    document = json.loads(_transcription().to_body())
    assert document["schema"] == TRANSCRIPTION_SCHEMA_V1
    assert "schema_name" not in document


# --- rejection ----------------------------------------------------------------


def test_unknown_schema_version_is_rejected() -> None:
    body = _transcription().to_body().replace(TRANSCRIPTION_SCHEMA_V1, "radio.transcription.v9")
    with pytest.raises(UnsupportedSchemaError):
        parse_transcription_job(body)


def test_analysis_rejects_a_transcription_schema() -> None:
    with pytest.raises(UnsupportedSchemaError):
        parse_analysis_job(_transcription().to_body())


def test_oversized_body_is_rejected_before_it_reaches_sqs() -> None:
    """Our ceiling is 64 KiB, well below the 1 MiB SQS limit.

    Built from the maximum permitted repeated fields, so the field-level caps
    and the byte-level ceiling are checked against each other rather than in
    isolation.
    """
    job = _analysis(
        matched_keywords=[
            MatchedKeywordRef(
                keyword_id=f"{index:08d}-0000-4000-8000-000000000000",
                canonical_value="न" * 200,  # multi-byte: 3 bytes per char in UTF-8
                matched_text="न" * 300,
                start_ms=0,
                end_ms=1,
            )
            for index in range(50)
        ],
        campaign_ids=[f"{index:08d}-1111-4111-8111-111111111111" for index in range(200)],
    )
    with pytest.raises(MessageTooLargeError):
        job.to_body()


def test_field_caps_are_enforced_independently_of_the_byte_ceiling() -> None:
    """Truncation must be an explicit decision, not an accident of encoding."""
    with pytest.raises(InvalidMessageError):
        document = json.loads(_analysis().to_body())
        document["campaign_ids"] = [
            f"{index:08d}-1111-4111-8111-111111111111" for index in range(201)
        ]
        parse_analysis_job(json.dumps(document))


def test_received_oversized_body_is_rejected_on_parse() -> None:
    with pytest.raises(MessageTooLargeError):
        parse_transcription_job("x" * (MAX_MESSAGE_BYTES + 1))


def test_malformed_json_is_a_permanent_error() -> None:
    error = pytest.raises(InvalidMessageError, parse_transcription_job, "{not json")
    assert error.value.retryable is False


def test_non_object_body_is_rejected() -> None:
    with pytest.raises(InvalidMessageError):
        parse_transcription_job("[1, 2, 3]")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"job_id": "not-a-uuid"}, "invalid uuid"),
        ({"station_id": "../etc/passwd"}, "path traversal in station id"),
        ({"station_id": "a" * 200}, "station id over the MessageGroupId limit"),
        ({"station_id": ""}, "empty station id"),
        ({"duration_ms": 0}, "zero duration"),
        ({"duration_ms": -1}, "negative duration"),
        ({"sequence_number": -1}, "negative sequence"),
        ({"content_class": "podcast"}, "unknown content class"),
        ({"keyword_index_version": -1}, "negative index version"),
        ({"language_hints": ["../"]}, "invalid language hint"),
    ],
)
def test_invalid_fields_are_rejected(overrides: dict, reason: str) -> None:
    document = json.loads(_transcription().to_body())
    document.update(overrides)
    with pytest.raises(InvalidMessageError):
        parse_transcription_job(json.dumps(document, default=str))


def test_extra_fields_are_rejected() -> None:
    """extra='forbid' means a v2 producer cannot silently confuse a v1 consumer."""
    document = json.loads(_transcription().to_body())
    document["surprise"] = "value"
    with pytest.raises(InvalidMessageError):
        parse_transcription_job(json.dumps(document))


def test_analysis_requires_at_least_one_campaign() -> None:
    document = json.loads(_analysis().to_body())
    document["campaign_ids"] = []
    with pytest.raises(InvalidMessageError):
        parse_analysis_job(json.dumps(document))


def test_matched_keyword_span_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="end_ms must not precede"):
        MatchedKeywordRef(
            keyword_id="99999999-9999-4999-8999-999999999999",
            canonical_value="NVIDIA",
            matched_text="NVIDIA",
            start_ms=1000,
            end_ms=500,
        )


# --- storage descriptor -------------------------------------------------------


def test_local_descriptor_requires_a_path() -> None:
    with pytest.raises(ValueError, match="local storage requires"):
        _storage(path=None)


def test_local_descriptor_rejects_bucket_and_key() -> None:
    with pytest.raises(ValueError, match="must not set"):
        _storage(bucket="b", key="k")


def test_s3_descriptor_requires_bucket_and_key() -> None:
    with pytest.raises(ValueError, match="s3 storage requires"):
        _storage(backend="s3", path=None, bucket="b", key=None)


def test_s3_key_must_not_be_a_uri() -> None:
    with pytest.raises(ValueError, match="plain object key"):
        _storage(backend="s3", path=None, bucket="b", key="s3://b/k")


def test_digest_must_be_hexadecimal() -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        _storage(sha256="z" * 64)


def test_unknown_storage_backend_is_rejected() -> None:
    document = json.loads(_transcription().to_body())
    document["storage"]["backend"] = "ftp"
    with pytest.raises(InvalidMessageError):
        parse_transcription_job(json.dumps(document))


# --- security -----------------------------------------------------------------


def test_no_credential_shaped_field_exists_on_the_wire() -> None:
    """A structural guard: secrets must be impossible, not merely absent."""
    forbidden = {
        "aws_access_key_id",
        "aws_secret_access_key",
        "presigned_url",
        "stream_url",
        "audio",
        "audio_bytes",
        "credentials",
        "token",
    }
    for model in (TranscriptionJobV1, AnalysisJobV1):
        assert not forbidden & set(model.model_fields)


def test_transcript_travels_by_reference_never_inline() -> None:
    assert "transcript_text" not in AnalysisJobV1.model_fields
    reference = AnalysisJobV1.model_fields["transcript_reference"]
    assert reference.annotation is TranscriptReference
