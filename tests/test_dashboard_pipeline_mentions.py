"""The dashboard must show the mentions the pipeline actually writes.

Production accumulated 73 real mentions in ``mention_events`` while every
dashboard endpoint queried the legacy v0.3 ``mentions`` table -- which the
shared-SQS pipeline never writes -- so the UI reported "Mentions / 7d: 0"
against 67 included mentions for the very campaign on screen.

The read path now serves BOTH stores through one row shape, and the detail
view for a pipeline mention is built from SQLite -- never from legacy S3
transcript keys, and never by re-running the LLM inside the API process.

Also covered here: subscriptions used to sit at 'starting' forever (nothing
advanced them), so capacity readouts lied while stations streamed for hours.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.analysis import MentionAnalysisService
from app.services.stream_supervisor import SessionStatus

NOW = datetime.now(UTC)
STAMP = NOW.isoformat()
MENTION_ID = "bea5139f-e495-44c2-8540-dabf9cd3e83a"
CONVERSATION_ID = "800ed4a5-2946-50da-b524-1071da0c55b2"
STATION_ID = "rb-78012206-1aa1-11e9-a80b-52543be04c81"


def seed_pipeline_mention(
    database,
    *,
    mention_id: str = MENTION_ID,
    included: int = 1,
    sentiment: str = "positive",
    status: str = "ready",
    with_analysis: bool = True,
    broadcast_at: datetime | None = None,
    stamp_transcript_conversation: bool = True,
    conversation_text: str | None = None,
) -> None:
    at = (broadcast_at or NOW).isoformat()

    def write(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO campaigns(id, name, objective, status,"
            " monitor_from_utc, created_at, updated_at) VALUES"
            " ('c1','Radio Test','brand_mentions','active',?,?,?)",
            (STAMP, STAMP, STAMP),
        )
        connection.execute(
            "INSERT OR IGNORE INTO campaign_keywords(id, campaign_id, entity_id,"
            " value, aliases_json, match_mode, keyword_type, semantic_matching,"
            " semantic_threshold, enabled)"
            " VALUES ('k1','c1','e1','night','[]','tokens','brand',0,0.74,1)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO station_subscriptions(station_id, display_name,"
            " country_code, language_codes_json, reference_count, state,"
            " shard_index, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,?,1,'starting',0,?,?)",
            (STATION_ID, "MANGORADIO", "DE", '["de"]', STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO station_sessions(station_session_id, station_id,"
            " generation, shard_index, status, started_at_utc)"
            " VALUES (?,?,1,0,'streaming',?)",
            (f"sess-{mention_id}", STATION_ID, at),
        )
        connection.execute(
            "INSERT INTO conversation_sessions(conversation_id, station_id,"
            " station_session_id, state, first_sequence_number,"
            " last_sequence_number, started_at_utc, transcript_text, trace_id,"
            " created_at_utc, updated_at_utc) VALUES (?,?,?,'closed',1,1,?,?,?,?,?)",
            (
                f"conv-{mention_id}", STATION_ID, f"sess-{mention_id}", at,
                conversation_text or "", "trace-1", at, at,
            ),
        )
        connection.execute(
            "INSERT INTO mention_events(mention_id, conversation_id, station_id,"
            " station_name, content_type, detected_language, broadcast_start_utc,"
            " broadcast_end_utc, transcript_id, trace_id, created_at_utc,"
            " updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mention_id, f"conv-{mention_id}", STATION_ID, "MANGORADIO",
                "speech", "en", at, at, f"t-{mention_id}", "trace-1", at, at,
            ),
        )
        connection.execute(
            "INSERT INTO mention_campaigns(mention_id, campaign_id, included,"
            " created_at_utc) VALUES (?,?,?,?)",
            (mention_id, "c1", included, at),
        )
        connection.execute(
            "INSERT INTO mention_keywords(mention_id, keyword_id, campaign_id,"
            " canonical_value, matched_text, match_level, confirmed,"
            " created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
            (mention_id, "k1", "c1", "night", "Night", "exact", 1, at),
        )
        if with_analysis:
            connection.execute(
                "INSERT INTO analysis_results(mention_id, analysis_job_id,"
                " status, model, summary, sentiment, key_points_json,"
                " evidence_json, confidence, needs_review, created_at_utc,"
                " updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mention_id, "job-1", status, "qwen3-0.6b-q8",
                    "A late night show mentioned the brand.", sentiment,
                    json.dumps(["Mentioned at night"]),
                    json.dumps([{"text": "good night everyone", "start_ms": 0, "end_ms": 900}]),
                    0.9, 0, at, at,
                ),
            )
        connection.execute(
            "INSERT INTO audio_segments(segment_id, station_id,"
            " station_session_id, sequence_number, started_at_utc, ended_at_utc,"
            " duration_ms, content_class, storage_backend, storage_path, sha256,"
            " size_bytes, disposition, trace_id, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,1,?,?,300000,'speech','local','x.opus','deadbeef',"
            " 1000,'retained','trace-1',?,?)",
            (f"seg-{mention_id}", STATION_ID, f"sess-{mention_id}", at, at, at, at),
        )
        connection.execute(
            "INSERT INTO transcripts(transcript_id, segment_id, station_id,"
            " conversation_id, asr_pass, text, detected_language, model_name,"
            " created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"t-{mention_id}", f"seg-{mention_id}", STATION_ID,
                # Production inserts transcripts BEFORE a conversation exists,
                # so the column is NULL until the close stamp runs.
                f"conv-{mention_id}" if stamp_transcript_conversation else None,
                "a",
                "Good night everyone, thanks for listening tonight.", "en",
                "faster-whisper", at,
            ),
        )

    database.write(write)


# =============================================================================
# The feed and the counts
# =============================================================================


def test_a_pipeline_mention_appears_in_the_feed(database) -> None:
    """The production bug: 67 included mentions, dashboard said 0."""
    seed_pipeline_mention(database)
    mentions, total = database.list_mentions()
    assert total == 1
    view = mentions[0]
    assert view["id"] == MENTION_ID
    assert view["campaign_name"] == "Radio Test"
    assert view["keyword"] == "night"
    assert view["matched_alias"] == "Night"
    assert view["station"]["name"] == "MANGORADIO"
    assert view["sentiment"]["label"] == "positive"
    assert view["context"] == "A late night show mentioned the brand."


def test_the_campaign_filter_counts_pipeline_mentions(database) -> None:
    """What "Mentions / 7d" on the campaign modal is built from."""
    seed_pipeline_mention(database)
    mentions, total = database.list_mentions(campaign_id="c1")
    assert total == 1 and mentions[0]["campaign_id"] == "c1"
    _, none_for_others = database.list_mentions(campaign_id="c-other")
    assert none_for_others == 0


def test_an_excluded_campaign_row_is_not_counted(database) -> None:
    """included=0 is a content-policy exclusion; the dashboard must not show
    a mention the policy excluded."""
    seed_pipeline_mention(database, included=0)
    _, total = database.list_mentions(campaign_id="c1")
    assert total == 0


def test_a_mention_without_analysis_defaults_to_neutral_needs_review(database) -> None:
    """The feed must not wait for the analysis worker to catch up."""
    seed_pipeline_mention(database, with_analysis=False)
    mentions, total = database.list_mentions()
    assert total == 1
    assert mentions[0]["sentiment"]["label"] == "neutral"
    assert mentions[0]["sentiment"]["needs_review"] is True


def test_mixed_sentiment_is_bucketed_as_neutral_in_the_feed(database) -> None:
    """The legacy feed vocabulary has no 'mixed'; the true value stays in the
    analysis view."""
    seed_pipeline_mention(database, sentiment="mixed")
    mentions, _ = database.list_mentions()
    assert mentions[0]["sentiment"]["label"] == "neutral"


def test_keyword_filter_narrows_the_feed_in_sql(database) -> None:
    """The modal filters by keyword across ALL pages, so it must be a SQL
    predicate: filtering the current page client-side would miss matches."""
    seed_pipeline_mention(database)
    _, matching = database.list_mentions(keywords=["night"])
    assert matching == 1
    _, other = database.list_mentions(keywords=["sale"])
    assert other == 0
    # Multi-select is an IN list, so any selected keyword keeps the row.
    _, either = database.list_mentions(keywords=["sale", "night"])
    assert either == 1


def test_no_keyword_filter_leaves_the_feed_untouched(database) -> None:
    seed_pipeline_mention(database)
    _, empty_list = database.list_mentions(keywords=[])
    _, no_filter = database.list_mentions()
    assert empty_list == no_filter == 1


def test_window_filter_matches_the_dashboard_headline_count(database) -> None:
    """The feed total and the campaign's windowed count disagreed (574 vs 446)
    because the feed counted all time. A since_utc bound makes them agree."""
    seed_pipeline_mention(database)
    seed_pipeline_mention(
        database,
        mention_id="11111111-2222-4333-8444-555555555555",
        broadcast_at=NOW - timedelta(days=30),
    )
    _, all_time = database.list_mentions(campaign_id="c1")
    assert all_time == 2

    since = (NOW - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    _, windowed = database.list_mentions(campaign_id="c1", since_utc=since)
    assert windowed == 1


def test_sentiment_summary_counts_pipeline_mentions(database) -> None:
    seed_pipeline_mention(database)
    summary = database.sentiment_summary()
    assert summary["positive"] == 1


def test_old_mentions_stay_counted_forever(database) -> None:
    """The dashboard reports all time, by request: users read a shrinking
    rolling window as their mentions being deleted."""
    seed_pipeline_mention(database, broadcast_at=NOW - timedelta(days=30))
    summary = database.sentiment_summary()
    assert summary["positive"] == 1  # a month-old mention still counts


def test_legacy_rows_and_pipeline_rows_share_the_feed(database) -> None:
    """Backward compatible means BOTH stores, not a migration cliff."""
    seed_pipeline_mention(database)
    database.upsert_mention(
        {
            "campaign_id": "c1",
            "campaign_keyword_id": "k1",
            "station_id": STATION_ID,
            "station_name": "MANGORADIO",
            "station_country_code": "DE",
            "station_language_codes": ["de"],
            "source_result_s3_key": "results/legacy.json",
            "source_mention_id": "legacy-1",
            "entity_id": "e1",
            "display_name": "night",
            "matched_alias": "night",
            "context": "legacy row",
            "detected_language": "en",
            "language_probability": 0.9,
            "sentiment_label": "negative",
            "sentiment_score": 0.2,
            "sentiment_margin": None,
            "needs_review": False,
            "broadcast_start_utc": STAMP,
            "broadcast_end_utc": STAMP,
            "audio_clip_start_utc": STAMP,
            "audio_clip_end_utc": STAMP,
            "audio_s3_key": "audio/legacy.opus",
            "raw_audio_s3_key": None,
            "transcript_s3_key": None,
        }
    )
    _, total = database.list_mentions()
    assert total == 2
    summary = database.sentiment_summary()
    assert summary["positive"] == 1 and summary["negative"] == 1


def test_a_pipeline_mention_reports_no_audio_yet(database) -> None:
    """evidence_available=0 -> the play button hides instead of breaking."""
    seed_pipeline_mention(database)
    view = database.mention_view_by_id(MENTION_ID)
    assert view is not None
    assert view["audio_available"] is False


def test_mention_audio_resolves_a_pipeline_mention_without_a_clip(database) -> None:
    """The audio-token route used to 404 on a mention the feed just showed,
    because mention_audio queried only the legacy table. It must resolve the
    mention and report the missing clip as None, never the string 'None'."""
    seed_pipeline_mention(database)
    reference = database.mention_audio(MENTION_ID)
    assert reference is not None
    assert reference["id"] == MENTION_ID
    assert reference["audio_s3_key"] is None


def test_campaign_mentions_7d_counts_pipeline_mentions(database) -> None:
    """The campaign card said "0 mentions / 7d" while the feed listed pipeline
    mentions: only the legacy table was counted."""
    seed_pipeline_mention(database)
    campaigns = database.list_campaigns()
    assert campaigns and campaigns[0]["id"] == "c1"
    assert campaigns[0]["mentions_7d"] == 1


def test_campaign_mentions_7d_skips_excluded_pipeline_rows(database) -> None:
    seed_pipeline_mention(database, included=0)
    campaigns = database.list_campaigns()
    assert campaigns and campaigns[0]["mentions_7d"] == 0


# =============================================================================
# The detail view
# =============================================================================


def build_service(settings, database) -> MentionAnalysisService:
    class ExplodingLegacyMachinery:
        """The legacy path must never run for a pipeline mention."""

        def __getattr__(self, name):
            raise AssertionError(f"legacy machinery invoked: {name}")

    return MentionAnalysisService(
        settings, database, ExplodingLegacyMachinery(),
        ExplodingLegacyMachinery(), ExplodingLegacyMachinery(),
    )


def test_detail_serves_the_committed_transcript_and_analysis(settings, database) -> None:
    seed_pipeline_mention(database)
    result = build_service(settings, database).detail(MENTION_ID)
    assert result is not None
    assert "Good night everyone" in result["full_transcript"]
    assert result["analysis"]["status"] == "ready"
    assert result["analysis"]["summary"] == "A late night show mentioned the brand."
    assert result["analysis"]["sentiment"] == "positive"
    assert result["analysis"]["evidence"] == ["good night everyone"]
    highlight = result["highlights"][0]
    assert highlight["keyword"] == "night"
    assert result["full_transcript"][
        highlight["start_char"] : highlight["end_char"]
    ].casefold() == "night"


def test_detail_for_a_fallback_analysis_reports_fallback(
    settings, database
) -> None:
    """A fallback analysis is still a usable record, but its summary is a
    transcript excerpt. Mapping it to 'ready' made the UI present the
    transcript as AI analysis with 0% confidence; the status now says so."""
    seed_pipeline_mention(database, status="fallback")
    result = build_service(settings, database).detail(MENTION_ID)
    assert result is not None
    assert result["analysis"]["status"] == "fallback"


def test_detail_falls_back_to_the_committed_conversation_text(
    settings, database
) -> None:
    """Production wrote transcripts with conversation_id=NULL (the close stamp
    did not exist), so the per-segment lookup found nothing and every pipeline
    mention rendered 'transcript no longer available'. The conversation's
    committed text is the durable copy; the detail view must serve it."""
    seed_pipeline_mention(
        database,
        stamp_transcript_conversation=False,
        conversation_text="Good night everyone, thanks for listening tonight.",
    )
    result = build_service(settings, database).detail(MENTION_ID)
    assert result is not None
    assert "Good night everyone" in result["full_transcript"]
    assert result["transcript_segments"], "the fallback must still render a segment"
    highlight = result["highlights"][0]
    assert result["full_transcript"][
        highlight["start_char"] : highlight["end_char"]
    ].casefold() == "night"


def test_detail_with_no_transcript_anywhere_is_empty_not_an_error(
    settings, database
) -> None:
    seed_pipeline_mention(
        database, stamp_transcript_conversation=False, conversation_text=None
    )
    result = build_service(settings, database).detail(MENTION_ID)
    assert result is not None
    assert result["full_transcript"] == ""


def test_fallback_analyses_heal_when_the_model_returns(settings, database) -> None:
    """A mention analysed during a model outage carries a grey fallback row
    forever; the idle sweep must replace it with the real analysis once the
    server answers its health probe."""
    from app.services.llm_analysis import AnalysisResult
    from app.services.result_writer import ResultWriter
    from app.workers.analysis import AnalysisWorker

    seed_pipeline_mention(
        database,
        status="fallback",
        conversation_text="Good night everyone, thanks for listening tonight.",
    )

    class HealingAnalyzer:
        def __init__(self) -> None:
            self.requests: list = []

        def healthy(self) -> bool:
            return True

        def analyze(self, request):
            self.requests.append(request)
            return AnalysisResult(
                summary="A real model summary.",
                sentiment="positive",
                confidence=0.9,
                needs_review=False,
                status="ready",
                model="test-model",
            )

    analyzer = HealingAnalyzer()
    worker = AnalysisWorker(
        settings,
        database,
        queue=object(),
        analyzer=analyzer,
        result_writer=ResultWriter(settings, database, s3_client=None),
    )
    worker._analysis_backlog()

    assert len(analyzer.requests) == 1
    assert "Good night everyone" in analyzer.requests[0].transcript
    view = database.mention_view_by_id(MENTION_ID)
    assert view is not None
    assert view["sentiment"]["label"] == "positive"
    assert view["sentiment"]["score"] == 0.9
    assert view["context"] == "A real model summary."
    row = database.pipeline_analysis_row(MENTION_ID)
    assert row is not None and row["status"] == "ready"


def test_healing_waits_for_a_healthy_model(settings, database) -> None:
    from app.services.result_writer import ResultWriter
    from app.workers.analysis import AnalysisWorker

    seed_pipeline_mention(
        database, status="fallback", conversation_text="Some committed speech."
    )

    class DownAnalyzer:
        def __init__(self) -> None:
            self.requests: list = []

        def healthy(self) -> bool:
            return False

        def analyze(self, request):  # pragma: no cover - must never run
            self.requests.append(request)
            raise AssertionError("analyze must not run while unhealthy")

    analyzer = DownAnalyzer()
    worker = AnalysisWorker(
        settings,
        database,
        queue=object(),
        analyzer=analyzer,
        result_writer=ResultWriter(settings, database, s3_client=None),
    )
    worker._analysis_backlog()
    assert analyzer.requests == []
    row = database.pipeline_analysis_row(MENTION_ID)
    assert row is not None and row["status"] == "fallback"


def test_a_failed_retry_is_not_hammered(settings, database) -> None:
    """A healthy probe followed by a failing request must stop the sweep and
    not retry the same mention every idle tick."""
    from app.services.llm_analysis import AnalysisResult
    from app.services.result_writer import ResultWriter
    from app.workers.analysis import AnalysisWorker

    seed_pipeline_mention(
        database, status="fallback", conversation_text="Some committed speech."
    )

    class StillBrokenAnalyzer:
        def __init__(self) -> None:
            self.requests: list = []

        def healthy(self) -> bool:
            return True

        def analyze(self, request):
            self.requests.append(request)
            return AnalysisResult(status="fallback", summary="excerpt")

    analyzer = StillBrokenAnalyzer()
    worker = AnalysisWorker(
        settings,
        database,
        queue=object(),
        analyzer=analyzer,
        result_writer=ResultWriter(settings, database, s3_client=None),
    )
    worker._analysis_backlog()
    worker._analysis_backlog()
    assert len(analyzer.requests) == 1
    row = database.pipeline_analysis_row(MENTION_ID)
    assert row is not None and row["status"] == "fallback"


def test_highlight_offsets_survive_german_eszett(settings, database) -> None:
    """The regression: highlights were located in a casefolded copy of the
    transcript, and casefolding expands every eszett to "ss", so each match
    after one marked the wrong characters ("Angebot" highlighted as
    "ngebot", "Germany" as "many, F"). Offsets must index the original."""
    text = "Der weiße Gruß an die Straße: night im Radio, gute Nacht."
    seed_pipeline_mention(
        database,
        stamp_transcript_conversation=False,
        conversation_text=text,
    )
    result = build_service(settings, database).detail(MENTION_ID)
    assert result is not None
    highlight = result["highlights"][0]
    span = result["full_transcript"][
        highlight["start_char"] : highlight["end_char"]
    ]
    assert span.casefold() == "night"


def test_reanalyse_never_reruns_the_llm_for_pipeline_mentions(settings, database) -> None:
    """The analysis worker owns pipeline analyses. The exploding stand-ins
    prove the API-side LLM path is never touched, even with force."""
    seed_pipeline_mention(database)
    result = build_service(settings, database).analyze(MENTION_ID, force=True)
    assert result is not None
    assert result["analysis"]["status"] == "ready"


# =============================================================================
# Subscription states stop lying
# =============================================================================


def make_listener(settings, database):
    from app.workers.listener import ListenerWorker

    return ListenerWorker(
        settings,
        database,
        segment_store=SimpleNamespace(usage_percent=lambda: 0.0),
    )


def test_a_streaming_session_promotes_its_subscription(settings, database) -> None:
    """Production showed active|1 starting|5 while four of those five were
    demonstrably producing mentions."""
    seed_pipeline_mention(database)
    worker = make_listener(settings, database)
    asyncio.run(
        worker.handle_status(
            SessionStatus(
                station_id=STATION_ID,
                station_session_id="sess-1",
                generation=1,
                status="streaming",
            )
        )
    )
    row = database.read_one(
        "SELECT state FROM station_subscriptions WHERE station_id=?", (STATION_ID,)
    )
    assert row is not None and row["state"] == "active"


def test_a_failing_session_degrades_its_subscription(settings, database) -> None:
    seed_pipeline_mention(database)
    worker = make_listener(settings, database)
    asyncio.run(
        worker.handle_status(
            SessionStatus(
                station_id=STATION_ID,
                station_session_id="sess-1",
                generation=1,
                status="streaming",
            )
        )
    )
    asyncio.run(
        worker.handle_status(
            SessionStatus(
                station_id=STATION_ID,
                station_session_id="sess-1",
                generation=2,
                status="failed",
                last_error="connection reset",
            )
        )
    )
    row = database.read_one(
        "SELECT state, state_reason FROM station_subscriptions WHERE station_id=?",
        (STATION_ID,),
    )
    assert row is not None and row["state"] == "degraded"
    assert "connection reset" in str(row["state_reason"])
