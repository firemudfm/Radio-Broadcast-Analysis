"""Result persistence: one mention, many mappings, idempotent under redelivery."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import Database
from app.services.conversation_assembler import ClosedConversation, TranscribedSegment
from app.services.keyword_matcher import KeywordMatch
from app.services.llm_analysis import AnalysisResult, Entity, Evidence
from app.services.result_writer import MentionContext, ResultWriter

START = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


class FakeS3:
    """Records puts so key determinism and encryption can be asserted."""

    def __init__(self, *, fail: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.fail = fail
        self.put_calls = 0

    def put_object(self, **kwargs):
        self.put_calls += 1
        if self.fail:
            raise RuntimeError("S3 is unavailable")
        self.objects[kwargs["Key"]] = kwargs
        return {"ETag": "etag"}


def match(keyword_id: str, *campaign_ids: str, level: str = "exact") -> KeywordMatch:
    return KeywordMatch(
        keyword_id=keyword_id,
        campaign_ids=campaign_ids,
        canonical_value=keyword_id.upper(),
        matched_text=keyword_id.upper(),
        match_level=level,  # type: ignore[arg-type]
        start_char=0,
        end_char=6,
        start_ms=1000,
        end_ms=2000,
        confidence=1.0,
    )


def conversation(
    *,
    conversation_id: str = "conversation-1",
    matches: tuple[KeywordMatch, ...] = (),
) -> ClosedConversation:
    segment = TranscribedSegment(
        segment_id="segment-1",
        station_id="rb-station",
        station_session_id="session-1",
        sequence_number=1,
        transcript_id="transcript-1",
        text="Buy the new NVIDIA laptop today",
        started_at=START,
        ended_at=START + timedelta(seconds=20),
        duration_ms=20_000,
        language="en",
    )
    return ClosedConversation(
        conversation_id=conversation_id,
        station_id="rb-station",
        station_session_id="session-1",
        close_reason="silence",
        first_sequence_number=1,
        last_sequence_number=1,
        started_at=START,
        ended_at=START + timedelta(seconds=20),
        duration_ms=20_000,
        transcript_text=segment.text,
        detected_language="en",
        segments=(segment,),
        matches=matches or (match("kw-nvidia", "campaign-a"),),
        missing_sequences=(),
        trace_id="trace-1",
    )


def analysis(**overrides) -> AnalysisResult:
    defaults = {
        "content_type": "advertisement",
        "language": "en",
        "relevant": True,
        "summary": "An NVIDIA laptop advertisement.",
        "sentiment": "positive",
        "speaker_stance": "promotional",
        "urgency": "normal",
        "entities": [Entity(name="NVIDIA", type="organization")],
        "key_points": ["promotion"],
        "evidence": [Evidence(text="the new NVIDIA laptop", start_ms=1000, end_ms=3000)],
        "confidence": 0.9,
        "model": "fake-qwen",
    }
    defaults.update(overrides)
    return AnalysisResult(**defaults)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        RADIO_S3_BUCKET="test-bucket",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def writer(settings: Settings, database: Database) -> ResultWriter:
    return ResultWriter(settings, database, s3_client=FakeS3())


# --- fan-out ------------------------------------------------------------------


def test_one_conversation_produces_one_mention_and_many_mappings(
    writer: ResultWriter, database: Database
) -> None:
    """The central claim: transcribe once, analyse once, attribute many times."""
    closed = conversation(
        matches=(
            match("kw-nvidia", "campaign-a", "campaign-b"),
            match("kw-amazon", "campaign-b", "campaign-c"),
        )
    )
    outcome = writer.persist(closed, analysis(), MentionContext(content_type="advertisement"))

    assert outcome.created
    assert outcome.campaign_rows == 3
    assert outcome.keyword_rows == 2

    mentions = database.read_all("SELECT * FROM mention_events")
    assert len(mentions) == 1, "one physical broadcast moment, one mention row"

    campaigns = database.read_all(
        "SELECT campaign_id FROM mention_campaigns ORDER BY campaign_id"
    )
    assert [row["campaign_id"] for row in campaigns] == [
        "campaign-a",
        "campaign-b",
        "campaign-c",
    ]

    analyses = database.read_all("SELECT * FROM analysis_results")
    assert len(analyses) == 1, "one analysis, shared by every campaign"


def test_mention_events_carries_no_campaign_or_keyword_column(database: Database) -> None:
    """Attribution must live only in the mapping tables."""
    columns = {
        str(row[1]) for row in database.read_all("PRAGMA table_info(mention_events)")
    }
    assert "campaign_id" not in columns
    assert "keyword_id" not in columns


# --- content policy -----------------------------------------------------------


def test_a_campaign_excluding_the_content_type_gets_an_excluded_row(
    writer: ResultWriter, database: Database
) -> None:
    """A song containing a brand leaves an auditable excluded row, not a hole."""
    closed = conversation(matches=(match("kw-amazon", "campaign-a", "campaign-b"),))
    outcome = writer.persist(
        closed,
        analysis(content_type="song_lyrics"),
        MentionContext(
            content_type="song_lyrics",
            campaign_policies={
                "campaign-a": {"include_song_lyrics": False},
                "campaign-b": {"include_song_lyrics": True},
            },
        ),
    )
    assert outcome.campaign_rows == 2
    assert outcome.included_campaign_rows == 1
    assert outcome.excluded_campaign_rows == 1

    excluded = database.read_one(
        "SELECT included, exclusion_reason FROM mention_campaigns WHERE campaign_id='campaign-a'"
    )
    assert excluded["included"] == 0
    assert "include_song_lyrics" in str(excluded["exclusion_reason"])


def test_the_global_default_excludes_song_lyrics(
    writer: ResultWriter, database: Database
) -> None:
    closed = conversation(matches=(match("kw-amazon", "campaign-a"),))
    outcome = writer.persist(
        closed, analysis(content_type="song_lyrics"), MentionContext(content_type="song_lyrics")
    )
    assert outcome.included_campaign_rows == 0


def test_unknown_content_stays_included(writer: ResultWriter) -> None:
    closed = conversation(matches=(match("kw-nvidia", "campaign-a"),))
    outcome = writer.persist(closed, analysis(), MentionContext(content_type="unknown"))
    assert outcome.included_campaign_rows == 1


# --- idempotency --------------------------------------------------------------


def test_a_redelivered_analysis_job_does_not_create_a_second_mention(
    writer: ResultWriter, database: Database
) -> None:
    closed = conversation()
    first = writer.persist(closed, analysis(), MentionContext(content_type="advertisement"))
    second = writer.persist(closed, analysis(), MentionContext(content_type="advertisement"))

    assert first.mention_id == second.mention_id
    assert first.created is True
    assert second.created is False
    assert len(database.read_all("SELECT 1 FROM mention_events")) == 1
    assert len(database.read_all("SELECT 1 FROM mention_campaigns")) == 1
    assert len(database.read_all("SELECT 1 FROM analysis_results")) == 1


def test_republishing_overwrites_the_same_deterministic_keys(
    settings: Settings, database: Database
) -> None:
    s3 = FakeS3()
    writer = ResultWriter(settings, database, s3_client=s3)
    closed = conversation()
    outcome = writer.persist(closed, analysis())

    first_key = writer.publish(outcome.mention_id, closed, analysis())
    keys_after_first = set(s3.objects)
    second_key = writer.publish(outcome.mention_id, closed, analysis())

    assert first_key == second_key
    assert set(s3.objects) == keys_after_first, "no duplicate objects accumulate"


# --- S3 documents -------------------------------------------------------------


def test_documents_are_written_with_deterministic_partitioned_keys(
    settings: Settings, database: Database
) -> None:
    s3 = FakeS3()
    writer = ResultWriter(settings, database, s3_client=s3)
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    key = writer.publish(outcome.mention_id, closed, analysis())

    assert key == f"mentions/2026/07/27/{outcome.mention_id}/metadata.json"
    assert set(s3.objects) == {
        f"mentions/2026/07/27/{outcome.mention_id}/metadata.json",
        f"mentions/2026/07/27/{outcome.mention_id}/transcript.json",
        f"mentions/2026/07/27/{outcome.mention_id}/analysis.json",
    }


def test_every_object_is_encrypted_and_has_no_acl(
    settings: Settings, database: Database
) -> None:
    s3 = FakeS3()
    writer = ResultWriter(settings, database, s3_client=s3)
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    writer.publish(outcome.mention_id, closed, analysis())

    for call in s3.objects.values():
        assert call["ServerSideEncryption"] == "AES256"
        assert "ACL" not in call
        assert call["ContentType"].startswith("application/json")


def test_the_stored_key_is_recorded_not_a_url(
    settings: Settings, database: Database
) -> None:
    s3 = FakeS3()
    writer = ResultWriter(settings, database, s3_client=s3)
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    writer.publish(outcome.mention_id, closed, analysis())

    stored = database.read_one(
        "SELECT result_s3_key FROM mention_events WHERE mention_id=?", (outcome.mention_id,)
    )
    key = str(stored["result_s3_key"])
    assert not key.startswith("http"), "a presigned URL is a credential with an untracked expiry"
    assert key.endswith("metadata.json")


def test_the_transcript_document_retains_the_original_language_and_evidence(
    settings: Settings, database: Database
) -> None:
    import json

    s3 = FakeS3()
    writer = ResultWriter(settings, database, s3_client=s3)
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    writer.publish(outcome.mention_id, closed, analysis())

    key = f"mentions/2026/07/27/{outcome.mention_id}/transcript.json"
    document = json.loads(s3.objects[key]["Body"].decode("utf-8"))
    assert document["text"] == "Buy the new NVIDIA laptop today"
    assert document["detected_language"] == "en"
    assert document["matches"][0]["matched_text"] == "KW-NVIDIA"
    assert document["segments"][0]["transcript_id"] == "transcript-1"


def test_model_metadata_is_retained(writer: ResultWriter, database: Database) -> None:
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    row = database.read_one(
        "SELECT model, schema_version, status FROM analysis_results WHERE mention_id=?",
        (outcome.mention_id,),
    )
    assert row["model"] == "fake-qwen"
    assert row["schema_version"] == "1"
    assert row["status"] == "ready"


# --- partial failure ----------------------------------------------------------


def test_an_s3_failure_leaves_the_sqlite_record_intact_and_retryable(
    settings: Settings, database: Database
) -> None:
    writer = ResultWriter(settings, database, s3_client=FakeS3(fail=True))
    closed = conversation()
    outcome = writer.persist(closed, analysis())

    assert writer.publish(outcome.mention_id, closed, analysis()) is None
    # The mention is still visible to the API; only the export is outstanding.
    assert database.read_one(
        "SELECT 1 FROM mention_events WHERE mention_id=?", (outcome.mention_id,)
    )
    assert outcome.mention_id in writer.unpublished_mentions()

    failures = database.read_all(
        "SELECT error_code, retryable FROM processing_failures WHERE component='result_writer'"
    )
    assert failures and failures[0]["error_code"] == "s3_publish_failed"
    assert failures[0]["retryable"] == 1


def test_publishing_is_skipped_without_an_s3_client(
    settings: Settings, database: Database
) -> None:
    writer = ResultWriter(settings, database, s3_client=None)
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    assert writer.publish(outcome.mention_id, closed, analysis()) is None
    assert outcome.mention_id in writer.unpublished_mentions()


def test_unpublished_list_clears_after_a_successful_publish(
    settings: Settings, database: Database
) -> None:
    writer = ResultWriter(settings, database, s3_client=FakeS3())
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    writer.publish(outcome.mention_id, closed, analysis())
    assert outcome.mention_id not in writer.unpublished_mentions()


# --- candidate matches --------------------------------------------------------


def test_unconfirmed_matches_are_recorded_as_unconfirmed(
    writer: ResultWriter, database: Database
) -> None:
    closed = conversation(matches=(match("kw-vw", "campaign-a", level="fuzzy"),))
    outcome = writer.persist(closed, analysis())
    row = database.read_one(
        "SELECT confirmed, match_level FROM mention_keywords WHERE mention_id=?",
        (outcome.mention_id,),
    )
    assert row["confirmed"] == 0
    assert row["match_level"] == "fuzzy"


def test_foreign_keys_link_mappings_to_the_mention(
    writer: ResultWriter, database: Database
) -> None:
    closed = conversation()
    outcome = writer.persist(closed, analysis())
    database.write(
        lambda connection: connection.execute(
            "DELETE FROM mention_events WHERE mention_id=?", (outcome.mention_id,)
        )
    )
    assert database.read_all("SELECT 1 FROM mention_campaigns") == []
    assert database.read_all("SELECT 1 FROM mention_keywords") == []
    assert database.read_all("SELECT 1 FROM analysis_results") == []
