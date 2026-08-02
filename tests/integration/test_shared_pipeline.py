"""End-to-end shared pipeline: campaigns -> segment -> transcript -> mention.

This is the test that proves the product claim. Three campaigns select the same
station; the audio is captured once, transcribed once and analysed once, and
the result is attributed to all three.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.pipeline.factory import ANALYSIS_QUEUE, TRANSCRIPTION_QUEUE
from app.pipeline.outbox import OutboxDispatcher
from app.services.stream_supervisor import SegmentEvent
from app.workers.analysis import AnalysisWorker
from app.workers.listener import ListenerWorker
from app.workers.planner import PlannerWorker
from app.workers.transcription import TranscriptionWorker
from tests.fixtures.campaigns import NOW, create_campaign

TRANSCRIPT = "Buy the new NVIDIA laptop today, available at all stores nationwide."

#: Station session ids are UUIDs in the wire contract, so tests use a real one.
SESSION = "11111111-2222-4333-8444-555555555555"

ANALYSIS_JSON = json.dumps(
    {
        "content_type": "advertisement",
        "language": "en",
        "relevant": True,
        "summary": "An advertisement for an NVIDIA laptop.",
        "sentiment": "positive",
        "speaker_stance": "promotional",
        "urgency": "normal",
        "entities": [{"name": "NVIDIA", "type": "organization"}],
        "key_points": ["Laptop promotion"],
        "evidence": [{"text": "the new NVIDIA laptop", "start_ms": 0, "end_ms": 3000}],
        "confidence": 0.9,
    }
)


def pcm(seconds: float = 20.0, sample_rate: int = 16_000) -> bytes:
    return b"\x10\x00" * int(seconds * sample_rate)


def segment_event(station_id: str, *, sequence: int = 1, session: str) -> SegmentEvent:
    from app.pipeline.ids import new_id

    return SegmentEvent(
        station_id=station_id,
        station_session_id=session,
        sequence_number=sequence,
        pcm=pcm(),
        sample_rate=16_000,
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=20),
        duration_ms=20_000,
        content_class="speech_over_music",
        content_class_confidence=0.8,
        classifier_signals={"low_energy_ratio": 0.3},
        keyword_index_version=1,
        trace_id=new_id(),
        generation=1,
    )


class Pipeline:
    """Drives the real workers step by step, deterministically."""

    def __init__(self, settings, database, spool, queues, transcript=TRANSCRIPT):
        from app.services.llm_analysis import ConversationAnalyzer, FakeLlmClient
        from app.services.transcription import FakeTranscriptionEngine, TranscriptionService

        self.settings = settings
        self.database = database
        self.queues = queues
        self.planner_worker = PlannerWorker(settings, database, queues=queues)
        self.listener = ListenerWorker(settings, database, segment_store=spool)
        engine = FakeTranscriptionEngine(responses={"*": transcript}, language="en")
        self.transcriber = TranscriptionWorker(
            settings,
            database,
            queue=queues[TRANSCRIPTION_QUEUE],
            segment_store=spool,
            transcription_service=TranscriptionService(
                settings, engine=engine, confirmation_engine=engine
            ),
        )
        self.llm = FakeLlmClient(responses=[ANALYSIS_JSON])
        self.analyser = AnalysisWorker(
            settings,
            database,
            queue=queues[ANALYSIS_QUEUE],
            analyzer=ConversationAnalyzer(settings, client=self.llm),
            s3_client=None,
        )
        self.dispatcher = OutboxDispatcher(database, queues)

    def plan(self):
        return self.planner_worker.planner.plan_once()

    def capture(self, event: SegmentEvent) -> None:
        self.listener._persist_segment(event)  # noqa: SLF001 - the durable half of capture

    def dispatch(self) -> dict:
        return self.dispatcher.dispatch_once()

    def transcribe(self) -> dict:
        return self.transcriber.processor.poll_once(self.transcriber.handle)

    def flush_conversations(self) -> None:
        self.transcriber.shutdown()

    def analyse(self) -> dict:
        return self.analyser.processor.poll_once(self.analyser.handle)

    def run_all(self, event: SegmentEvent) -> None:
        self.plan()
        self.capture(event)
        self.dispatch()
        self.transcribe()
        self.flush_conversations()
        self.dispatch()
        self.analyse()


@pytest.fixture
def station(database):
    """A station subscription with a resolved stream URL."""
    station_id = "rb-shared-station"

    def register() -> str:
        database.write(
            lambda connection: connection.execute(
                "UPDATE station_subscriptions SET stream_url=?, display_name=?,"
                " language_codes_json=? WHERE station_id=?",
                ("https://stream.example.com/live", "Shared FM", '["en"]', station_id),
            )
        )
        return station_id

    return station_id, register


# --- the central claim --------------------------------------------------------


def test_three_campaigns_sharing_a_station_yield_one_mention_three_attributions(
    settings, database, spool, queues, station
):
    station_id, register = station
    for name in ("Campaign A", "Campaign B", "Campaign C"):
        create_campaign(
            database, name=name, station_ids=[station_id], keywords=[("NVIDIA", "brand")]
        )

    pipeline = Pipeline(settings, database, spool, queues)
    plan = pipeline.plan()
    register()

    # One subscription for the station, whatever the campaign count.
    subscriptions = database.read_all("SELECT * FROM station_subscriptions")
    assert len(subscriptions) == 1
    assert subscriptions[0]["reference_count"] == 3
    assert plan.unique_requested == 1
    assert plan.reused_station_streams == 1

    # One combined keyword index, not one per campaign-station pair.
    indexes = database.read_all("SELECT * FROM station_keyword_index_versions")
    assert len(indexes) == 1
    assert indexes[0]["campaign_count"] == 3
    assert indexes[0]["keyword_count"] == 3

    pipeline.capture(segment_event(station_id, session=SESSION))
    assert pipeline.dispatch()["sent"] == 1
    assert len(database.read_all("SELECT 1 FROM audio_segments")) == 1

    assert pipeline.transcribe()["processed"] == 1
    # One transcript for the segment, shared by every campaign.
    assert len(database.read_all("SELECT 1 FROM transcripts")) == 1

    pipeline.flush_conversations()
    assert pipeline.dispatch()["sent"] == 1
    assert pipeline.analyse()["processed"] == 1

    # One physical mention, one analysis, three attributions.
    mentions = database.read_all("SELECT * FROM mention_events")
    assert len(mentions) == 1

    analyses = database.read_all("SELECT * FROM analysis_results")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "ready"
    assert analyses[0]["sentiment"] == "positive"

    campaigns = database.read_all(
        "SELECT campaign_id, included FROM mention_campaigns ORDER BY campaign_id"
    )
    assert len(campaigns) == 3
    assert all(row["included"] == 1 for row in campaigns)

    assert len(pipeline.llm.calls) == 1, "the LLM runs once per conversation, not per campaign"


def test_a_second_station_does_not_share_the_first_stations_index(
    settings, database, spool, queues
):
    create_campaign(
        database, name="Alpha", station_ids=["rb-one"], keywords=[("NVIDIA", "brand")]
    )
    create_campaign(
        database, name="Beta", station_ids=["rb-two"], keywords=[("Amazon", "brand")]
    )
    Pipeline(settings, database, spool, queues).plan()

    assert len(database.read_all("SELECT 1 FROM station_subscriptions")) == 2
    versions = {
        str(row["station_id"]): json.loads(str(row["payload_json"]))
        for row in database.read_all("SELECT station_id, payload_json FROM station_keyword_index_versions")
    }
    assert [entry["canonical_value"] for entry in versions["rb-one"]["entries"]] == ["NVIDIA"]
    assert [entry["canonical_value"] for entry in versions["rb-two"]["entries"]] == ["Amazon"]


# --- keyword fan-out ----------------------------------------------------------


def test_one_transcript_matching_two_campaigns_keywords_produces_one_analysis(
    settings, database, spool, queues, station
):
    station_id, register = station
    create_campaign(
        database, name="Chips", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    create_campaign(
        database, name="Retail", station_ids=[station_id], keywords=[("laptop", "product")]
    )

    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    register()
    pipeline.run_all(segment_event(station_id, session=SESSION))

    assert len(database.read_all("SELECT 1 FROM mention_events")) == 1
    assert len(database.read_all("SELECT 1 FROM analysis_jobs")) == 0  # queued via outbox
    keywords = database.read_all(
        "SELECT canonical_value FROM mention_keywords ORDER BY canonical_value"
    )
    assert [row["canonical_value"] for row in keywords] == ["NVIDIA", "laptop"]
    campaigns = database.read_all("SELECT 1 FROM mention_campaigns")
    assert len(campaigns) == 2
    assert len(pipeline.llm.calls) == 1


def test_a_transcript_with_no_keyword_creates_no_mention(
    settings, database, spool, queues, station
):
    station_id, register = station
    create_campaign(
        database, name="Alpha", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    pipeline = Pipeline(
        settings, database, spool, queues, transcript="today's weather is mild and clear"
    )
    pipeline.plan()
    register()
    pipeline.run_all(segment_event(station_id, session=SESSION))

    assert database.read_all("SELECT 1 FROM mention_events") == []
    assert pipeline.llm.calls == [], "no keyword means no analysis spend"
    # The audio is marked disposable so cleanup can reclaim it.
    segment = database.read_one("SELECT disposition FROM audio_segments")
    assert segment["disposition"] == "disposable"


# --- reliability --------------------------------------------------------------


def test_a_redelivered_segment_does_not_duplicate_work(
    settings, database, spool, queues, station
):
    station_id, register = station
    create_campaign(
        database, name="Alpha", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    register()

    event = segment_event(station_id, session=SESSION)
    pipeline.capture(event)
    pipeline.dispatch()
    assert pipeline.transcribe()["processed"] == 1

    queue = queues[TRANSCRIPTION_QUEUE]
    body = str(
        database.read_one(
            "SELECT payload_json FROM outbox_events WHERE queue_name=?",
            (TRANSCRIPTION_QUEUE,),
        )["payload_json"]
    )
    segment_id = json.loads(body)["segment_id"]

    # Layer 1: inside the 5-minute window, the queue itself drops a duplicate
    # send. Nothing is delivered, so nothing needs deduplicating downstream.
    queue.send(body, group_id=station_id, deduplication_id=segment_id)
    assert queue.approximate_depth() == 0
    assert pipeline.transcribe()["received"] == 0

    # Layer 2: the real guarantee. SQS FIFO deduplication expires after five
    # minutes, so a redelivery after a longer outage WOULD arrive. Clearing the
    # window simulates exactly that, and the consumer inbox must absorb it.
    queue._dedup.clear()  # noqa: SLF001 - simulating the dedup window expiring
    queue.send(body, group_id=station_id, deduplication_id=segment_id)
    assert queue.approximate_depth() == 1

    result = pipeline.transcribe()
    assert result["duplicate"] == 1
    assert result["processed"] == 0
    assert len(database.read_all("SELECT 1 FROM transcripts")) == 1, (
        "a redelivered segment must not be transcribed twice"
    )


def test_a_replay_after_a_worker_restart_does_not_fork_a_second_mention(
    settings, database, spool, queues, station
):
    """The hard case: a restarted worker has no in-memory assembler state.

    Without a deterministic conversation id it would open a *new* conversation
    for the replayed segment and produce a duplicate mention, and the inbox
    could not help because a fresh conversation id dedupes against nothing.
    """
    station_id, register = station
    create_campaign(
        database, name="Alpha", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    first = Pipeline(settings, database, spool, queues)
    first.plan()
    register()
    first.capture(segment_event(station_id, session=SESSION))
    first.dispatch()
    first.transcribe()
    first.flush_conversations()
    first.dispatch()
    first.analyse()

    assert len(database.read_all("SELECT 1 FROM mention_events")) == 1
    original = database.read_one("SELECT conversation_id FROM mention_events")["conversation_id"]

    # A brand-new worker set: fresh assembler, fresh matcher cache, empty state.
    replayed = Pipeline(settings, database, spool, queues)
    body = str(
        database.read_one(
            "SELECT payload_json FROM outbox_events WHERE queue_name=?",
            (TRANSCRIPTION_QUEUE,),
        )["payload_json"]
    )
    queue = queues[TRANSCRIPTION_QUEUE]
    queue._dedup.clear()  # noqa: SLF001 - simulating the dedup window expiring
    database.write(lambda connection: connection.execute("DELETE FROM inbox_messages"))
    queue.send(body, group_id=station_id, deduplication_id=json.loads(body)["segment_id"])

    replayed.transcribe()
    replayed.flush_conversations()
    replayed.dispatch()
    replayed.analyse()

    mentions = database.read_all("SELECT conversation_id FROM mention_events")
    assert len(mentions) == 1, "a replay must collapse into the original mention"
    assert mentions[0]["conversation_id"] == original
    assert len(database.read_all("SELECT 1 FROM mention_campaigns")) == 1


def test_the_outbox_holds_messages_until_dispatch(
    settings, database, spool, queues, station
):
    """Capture commits the intent to send; nothing reaches the queue until the
    dispatcher runs. That is the window the outbox exists to close."""
    station_id, register = station
    create_campaign(
        database, name="Alpha", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    register()
    pipeline.capture(segment_event(station_id, session=SESSION))

    pending = database.read_all("SELECT status FROM outbox_events")
    assert [row["status"] for row in pending] == ["pending"]
    assert queues[TRANSCRIPTION_QUEUE].approximate_depth() == 0

    pipeline.dispatch()
    assert queues[TRANSCRIPTION_QUEUE].approximate_depth() == 1
    sent = database.read_all("SELECT status, sqs_message_id FROM outbox_events")
    assert sent[0]["status"] == "sent"
    assert sent[0]["sqs_message_id"]


def test_segment_bytes_land_on_disk_before_the_job_row_exists(
    settings, database, spool, queues, station
):
    station_id, register = station
    create_campaign(
        database, name="Alpha", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    register()
    pipeline.capture(segment_event(station_id, session=SESSION))

    row = database.read_one("SELECT storage_path, sha256, size_bytes FROM audio_segments")
    from pathlib import Path

    path = Path(str(row["storage_path"]))
    assert path.is_file()
    assert path.stat().st_size == row["size_bytes"]

    import hashlib

    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]


def test_message_group_id_is_the_station_so_segments_stay_ordered(
    settings, database, spool, queues, station
):
    station_id, register = station
    create_campaign(
        database, name="Alpha", station_ids=[station_id], keywords=[("NVIDIA", "brand")]
    )
    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    register()
    pipeline.capture(segment_event(station_id, sequence=1, session=SESSION))
    pipeline.capture(segment_event(station_id, sequence=2, session=SESSION))

    groups = database.read_all(
        "SELECT DISTINCT message_group_id FROM outbox_events WHERE queue_name=?",
        (TRANSCRIPTION_QUEUE,),
    )
    assert [row["message_group_id"] for row in groups] == [station_id]

    pipeline.dispatch()
    # FIFO: one in-flight message per group, so ordering is preserved.
    first = queues[TRANSCRIPTION_QUEUE].receive(max_messages=10)
    assert len(first) == 1


# --- content policy -----------------------------------------------------------


def test_a_song_lyric_mention_is_excluded_for_a_campaign_that_disallows_it(
    settings, database, spool, queues, station
):
    station_id, register = station
    campaign_id = create_campaign(
        database, name="Retail", station_ids=[station_id], keywords=[("Amazon", "brand")]
    )
    database.write(
        lambda connection: connection.execute(
            "INSERT INTO campaign_content_policies(campaign_id, policy_json,"
            " created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?)",
            (campaign_id, json.dumps({"include_song_lyrics": False}), "now", "now"),
        )
    )

    lyric = "Amazon river flowing Amazon river flowing down the Amazon river flowing on"
    pipeline = Pipeline(settings, database, spool, queues, transcript=lyric)
    pipeline.plan()
    register()

    event = segment_event(station_id, session=SESSION)
    sung = SegmentEvent(**{**event.__dict__, "content_class": "singing"})
    pipeline.run_all(sung)

    row = database.read_one(
        "SELECT included, exclusion_reason FROM mention_campaigns WHERE campaign_id=?",
        (campaign_id,),
    )
    assert row is not None, "the physical event is still recorded"
    assert row["included"] == 0
    assert "include_song_lyrics" in str(row["exclusion_reason"])


# --- capacity -----------------------------------------------------------------


def test_stations_beyond_capacity_are_parked_not_dropped(settings, database, spool, queues):
    tuned = settings.model_copy(
        update={"RADIO_MAX_ACTIVE_UNIQUE_STATIONS": 2, "RADIO_LISTENER_MAX_SESSIONS": 2}
    )
    for index in range(5):
        create_campaign(
            database,
            name=f"Campaign {index}",
            station_ids=[f"rb-station-{index}"],
            keywords=[("NVIDIA", "brand")],
        )
    plan = Pipeline(tuned, database, spool, queues).plan()

    assert plan.unique_requested == 5
    assert plan.unique_active == 2
    assert plan.pending_capacity == 3

    parked = database.read_all(
        "SELECT state_reason FROM station_subscriptions WHERE state='pending_capacity'"
    )
    assert len(parked) == 3
    assert all("limit reached" in str(row["state_reason"]) for row in parked)


def test_capacity_counters_distinguish_their_meanings(settings, database, spool, queues):
    # Two active slots, stated explicitly. The production default is 1, and this
    # test is about the counters meaning DIFFERENT things -- which only shows up
    # when more than one station can be active at once.
    settings = settings.model_copy(
        update={"RADIO_MAX_ACTIVE_UNIQUE_STATIONS": 2, "RADIO_LISTENER_MAX_SESSIONS": 2}
    )
    for index in range(3):
        create_campaign(
            database,
            name=f"Campaign {index}",
            # Every campaign selects the same two stations.
            station_ids=["rb-a", "rb-b"],
            keywords=[("NVIDIA", "brand"), ("Amazon", "brand")],
        )
    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    snapshot = pipeline.planner_worker.planner.capacity_snapshot()

    assert snapshot["campaign_station_reference_count"] == 6, "3 campaigns x 2 stations"
    assert snapshot["unique_requested_station_count"] == 2, "only two distinct stations"
    assert snapshot["unique_active_station_count"] == 2
    assert snapshot["reused_station_stream_count"] == 2
    assert snapshot["pending_capacity_station_count"] == 0


def test_removing_a_campaign_keeps_a_station_others_still_reference(
    settings, database, spool, queues
):
    first = create_campaign(
        database, name="Alpha", station_ids=["rb-shared"], keywords=[("NVIDIA", "brand")]
    )
    create_campaign(
        database, name="Beta", station_ids=["rb-shared"], keywords=[("Amazon", "brand")]
    )
    pipeline = Pipeline(settings, database, spool, queues)
    pipeline.plan()
    assert database.read_one(
        "SELECT reference_count FROM station_subscriptions WHERE station_id='rb-shared'"
    )["reference_count"] == 2

    database.delete_campaign(first)
    pipeline.plan()

    row = database.read_one(
        "SELECT reference_count, state FROM station_subscriptions WHERE station_id='rb-shared'"
    )
    assert row["reference_count"] == 1
    assert row["state"] != "stopped", "N -> 1 must keep the station listening"
