"""Keyword evidence survives matcher -> queue -> analysis -> mention_keywords.

The unit tests in ``tests/unit/test_result_writer.py`` call ``persist()``
directly and therefore never cross the serialisation boundary -- which is
exactly why the evidence-loss defect survived them. Everything here goes
through the real producer, a real serialised ``AnalysisJobV1``, real
``parse_analysis_job``, and the real analysis worker, then reads the persisted
database row rather than an in-memory object.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.pipeline.contracts import MatchedKeywordRef, parse_analysis_job
from app.pipeline.factory import ANALYSIS_QUEUE, TRANSCRIPTION_QUEUE
from app.pipeline.outbox import OutboxDispatcher
from app.services.conversation_assembler import ConversationAssembler, TranscribedSegment
from app.services.keyword_index import build_index
from app.services.keyword_matcher import KeywordMatch, KeywordMatcher
from app.services.llm_analysis import AnalysisResult
from app.services.result_writer import MentionContext, ResultWriter
from app.services.subscription_planner import SubscriptionPlanner
from app.workers.transcription import TranscriptionWorker
from tests.fixtures.campaigns import NOW, create_campaign

SESSION = "11111111-2222-4333-8444-555555555555"
STATION = "rb-evidence"

ANALYSIS_JSON = json.dumps(
    {
        "content_type": "advertisement",
        "language": "hi",
        "relevant": True,
        "summary": "An NVIDIA advertisement.",
        "sentiment": "positive",
        "confidence": 0.8,
    }
)


def binding(keyword_id: str, campaign_id: str, value: str, *, aliases=None) -> dict:
    return {
        "keyword_id": keyword_id,
        "campaign_id": campaign_id,
        "entity_id": value.lower(),
        "canonical_value": value,
        "keyword_type": "brand",
        "match_mode": "tokens",
        "aliases": aliases or [],
        "languages": [],
        "content_policy": {},
    }


def segment(index: int, text: str, matches=(), *, seconds: int = 20) -> TranscribedSegment:
    started = NOW + timedelta(seconds=seconds * index)
    return TranscribedSegment(
        segment_id=f"segment-{index}",
        station_id=STATION,
        station_session_id=SESSION,
        sequence_number=index,
        # The wire contract validates transcript_id as a UUID, so the fixture
        # has to produce a real one rather than a readable label.
        transcript_id=f"{index:08x}-7777-4777-8777-777777777777",
        text=text,
        started_at=started,
        ended_at=started + timedelta(seconds=seconds),
        duration_ms=seconds * 1000,
        language="hi",
        matches=tuple(matches),
    )


def close_conversation(settings, segments):
    assembler = ConversationAssembler(settings)
    for item in segments:
        assembler.observe(item)
    closed = assembler.close(STATION, reason="silence")
    assert closed is not None
    return closed


# --- F. conversation-relative coordinates -------------------------------------


def test_match_spans_are_conversation_relative(settings):
    """Pre-roll + keyword segment + trailing segment.

    Before this fix the matcher's segment-relative offsets were published
    unchanged, so slicing the assembled transcript returned arbitrary text from
    an earlier segment.
    """
    matcher = KeywordMatcher(build_index(STATION, [binding("kw-1", "camp-a", "NVIDIA")]))
    texts = [
        "Welcome back to the programme today",
        "The new NVIDIA card was announced",
        "and analysts responded positively",
    ]
    segments = [segment(i, t, matcher.match(t).matches) for i, t in enumerate(texts)]
    closed = close_conversation(settings, segments)

    match = closed.matches[0]
    assert closed.transcript_text[match.start_char : match.end_char] == match.matched_text
    assert match.start_char == closed.transcript_text.index("NVIDIA")

    # No span may fall outside the assembled transcript.
    assert 0 <= match.start_char < match.end_char <= len(closed.transcript_text)


def test_millisecond_spans_are_rebased_onto_the_conversation(settings):
    from app.services.keyword_matcher import Timeline, TimelineEntry

    matcher = KeywordMatcher(build_index(STATION, [binding("kw-1", "camp-a", "NVIDIA")]))
    text = "The new NVIDIA card was announced"
    # Timings inside the second segment, measured from that segment's start.
    timeline = Timeline([TimelineEntry(0, len(text), start_ms=1_000, end_ms=2_500)])
    segments = [
        segment(0, "Welcome back to the programme today"),
        segment(1, text, matcher.match(text, timeline=timeline).matches),
    ]
    closed = close_conversation(settings, segments)

    match = closed.matches[0]
    # Segment 1 starts 20s into the conversation, so 1000ms becomes 21000ms.
    assert match.start_ms == 21_000
    assert match.end_ms == 22_500
    assert match.start_ms >= 0
    assert match.end_ms <= closed.duration_ms


def test_a_blank_segment_does_not_shift_the_character_base(settings):
    matcher = KeywordMatcher(build_index(STATION, [binding("kw-1", "camp-a", "NVIDIA")]))
    text = "The NVIDIA launch"
    segments = [
        segment(0, "   "),  # excluded from transcript_text entirely
        segment(1, text, matcher.match(text).matches),
    ]
    closed = close_conversation(settings, segments)
    match = closed.matches[0]
    assert closed.transcript_text[match.start_char : match.end_char] == "NVIDIA"


def test_leading_whitespace_in_a_segment_is_accounted_for(settings):
    matcher = KeywordMatcher(build_index(STATION, [binding("kw-1", "camp-a", "NVIDIA")]))
    text = "    The NVIDIA launch"  # stripped when joined
    segments = [segment(0, "Earlier today"), segment(1, text, matcher.match(text).matches)]
    closed = close_conversation(settings, segments)
    match = closed.matches[0]
    assert closed.transcript_text[match.start_char : match.end_char] == "NVIDIA"


# --- C. alias round trip ------------------------------------------------------


def test_alias_match_survives_the_full_round_trip(settings, database, spool, queues):
    """A Hindi native-script alias must not be recorded as an exact match."""
    campaign_id = create_campaign(
        database,
        name="Hindi Campaign",
        station_ids=[STATION],
        keywords=[("NVIDIA", "brand")],
    )
    # Attach the native-script alias the way the planner reads it.
    database.write(
        lambda c: c.execute(
            "UPDATE campaign_keywords SET aliases_json=? WHERE campaign_id=?",
            (json.dumps([{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}]), campaign_id),
        )
    )
    SubscriptionPlanner(settings, database).plan_once()

    matcher = KeywordMatcher(SubscriptionPlanner(settings, database).keyword_index_for(STATION))
    text = "आज एनवीडिया ने घोषणा की"
    report = matcher.match(text)
    assert report.matches[0].match_level == "alias", "precondition: matcher found an alias"
    source = report.matches[0]

    closed = close_conversation(settings, [segment(0, "पहले", ()), segment(1, text, report.matches)])

    # Producer -> wire -> consumer, using the real worker code paths.
    worker = TranscriptionWorker(
        settings, database, queue=queues[TRANSCRIPTION_QUEUE], segment_store=spool
    )
    worker._commit_conversation(closed)  # noqa: SLF001 - the producer under test
    body = str(
        database.read_one(
            "SELECT payload_json FROM outbox_events WHERE queue_name=?", (ANALYSIS_QUEUE,)
        )["payload_json"]
    )
    job = parse_analysis_job(body)
    ref = job.matched_keywords[0]

    assert ref.matched_text == "एनवीडिया"
    assert ref.match_level == "alias", "must not be flattened to exact"
    assert ref.confidence == pytest.approx(source.confidence)
    assert ref.campaign_ids == [campaign_id]
    assert closed.transcript_text[ref.start_char : ref.resolved_end_char] == "एनवीडिया"


def test_alias_match_lands_correctly_in_mention_keywords(
    settings, database, spool, queues, analysis_worker
):
    """Phase 10: assert the persisted row, not an in-memory object."""
    campaign_id = create_campaign(
        database, name="Hindi Campaign", station_ids=[STATION], keywords=[("NVIDIA", "brand")]
    )
    database.write(
        lambda c: c.execute(
            "UPDATE campaign_keywords SET aliases_json=? WHERE campaign_id=?",
            (json.dumps([{"value": "एनवीडिया", "kind": "native_script"}]), campaign_id),
        )
    )
    SubscriptionPlanner(settings, database).plan_once()
    matcher = KeywordMatcher(SubscriptionPlanner(settings, database).keyword_index_for(STATION))
    text = "आज एनवीडिया ने घोषणा की"
    closed = close_conversation(settings, [segment(0, text, matcher.match(text).matches)])

    TranscriptionWorker(
        settings, database, queue=queues[TRANSCRIPTION_QUEUE], segment_store=spool
    )._commit_conversation(closed)  # noqa: SLF001
    OutboxDispatcher(database, queues).dispatch_once()
    assert analysis_worker.processor.poll_once(analysis_worker.handle)["processed"] == 1

    row = database.read_one(
        "SELECT match_level, confirmed, confidence, matched_text, start_char, end_char,"
        " campaign_id FROM mention_keywords"
    )
    assert row["match_level"] == "alias", "the permanent audit trail must say alias"
    assert row["confirmed"] == 1, "alias does not require pass-B confirmation"
    assert row["confidence"] == pytest.approx(0.95)
    assert row["matched_text"] == "एनवीडिया"
    assert row["campaign_id"] == campaign_id
    assert closed.transcript_text[row["start_char"] : row["end_char"]] == "एनवीडिया"


# --- D. candidate round trip --------------------------------------------------


def test_a_fuzzy_candidate_is_not_recorded_as_confirmed(database, settings):
    """Built directly; production fuzzy matching stays disabled."""
    writer = ResultWriter(settings, database, s3_client=None)
    candidate = KeywordMatch(
        keyword_id="kw-vw",
        campaign_ids=("camp-a",),
        canonical_value="Volkswagen",
        matched_text="volkswagon",
        match_level="fuzzy",
        start_char=4,
        end_char=14,
        start_ms=1_000,
        end_ms=2_000,
        confidence=0.55,
    )
    closed = close_conversation(settings, [segment(0, "the volkswagon dealership", [candidate])])
    assert closed.requires_confirmation

    outcome = writer.persist(
        closed,
        AnalysisResult(summary="x", sentiment="neutral", relevant=True, confidence=0.5),
        MentionContext(content_type="unknown"),
    )
    row = database.read_one(
        "SELECT match_level, confirmed, confidence FROM mention_keywords WHERE mention_id=?",
        (outcome.mention_id,),
    )
    assert row["match_level"] == "fuzzy", "must not be silently converted to exact"
    assert row["confirmed"] == 0, "a candidate is not confirmed evidence"
    assert row["confidence"] == pytest.approx(0.55)


# --- E. campaign ownership ----------------------------------------------------


def test_each_keyword_keeps_only_its_own_campaign(settings, database, spool, queues):
    """Campaign A tracks NVIDIA, Campaign B tracks GPU, one conversation."""
    campaign_a = create_campaign(
        database, name="Chips Campaign", station_ids=[STATION], keywords=[("NVIDIA", "brand")]
    )
    campaign_b = create_campaign(
        database, name="Parts Campaign", station_ids=[STATION], keywords=[("GPU", "product")]
    )
    SubscriptionPlanner(settings, database).plan_once()
    matcher = KeywordMatcher(SubscriptionPlanner(settings, database).keyword_index_for(STATION))

    text = "the NVIDIA GPU launch"
    closed = close_conversation(settings, [segment(0, text, matcher.match(text).matches)])
    assert set(closed.campaign_ids) == {campaign_a, campaign_b}

    TranscriptionWorker(
        settings, database, queue=queues[TRANSCRIPTION_QUEUE], segment_store=spool
    )._commit_conversation(closed)  # noqa: SLF001
    body = str(
        database.read_one(
            "SELECT payload_json FROM outbox_events WHERE queue_name=?", (ANALYSIS_QUEUE,)
        )["payload_json"]
    )
    job = parse_analysis_job(body)

    by_value = {ref.canonical_value: ref for ref in job.matched_keywords}
    assert by_value["NVIDIA"].campaign_ids == [campaign_a]
    assert by_value["GPU"].campaign_ids == [campaign_b]
    assert sorted(job.campaign_ids) == sorted([campaign_a, campaign_b]), "job-level is the union"

    # Reconstruction must not broaden ownership back out again.
    for ref in job.matched_keywords:
        resolved = ref.resolved_campaign_ids(job.campaign_ids)
        assert len(resolved) == 1, "each keyword owns exactly one campaign here"


def test_two_campaigns_tracking_one_word_share_the_scan_not_the_keyword_id(
    settings, database, spool, queues
):
    """Two campaigns both tracking NVIDIA.

    `campaign_keywords.id` is a fresh UUID per campaign, so two campaigns
    tracking the same word own two distinct keyword records. The sharing
    happens one level down: the index collapses them into a SINGLE scan term,
    so the transcript is still scanned once.

    What must hold regardless: one physical mention, one analysis job, and each
    keyword row attributed to its own campaign rather than to both.
    """
    campaign_a = create_campaign(
        database, name="Campaign Alpha", station_ids=[STATION], keywords=[("NVIDIA", "brand")]
    )
    campaign_b = create_campaign(
        database, name="Campaign Beta", station_ids=[STATION], keywords=[("NVIDIA", "brand")]
    )
    SubscriptionPlanner(settings, database).plan_once()
    index = SubscriptionPlanner(settings, database).keyword_index_for(STATION)

    assert index.keyword_count == 2, "one keyword record per campaign"
    assert len(index.terms) == 1, "but only ONE scan term -- the transcript is scanned once"

    text = "the NVIDIA launch"
    matches = KeywordMatcher(index).match(text).matches
    assert len(matches) == 2
    owners = {m.keyword_id: m.campaign_ids for m in matches}
    assert sorted(c for ids in owners.values() for c in ids) == sorted([campaign_a, campaign_b])
    assert all(len(ids) == 1 for ids in owners.values()), "no cross-attribution"

    closed = close_conversation(settings, [segment(0, text, matches)])
    TranscriptionWorker(
        settings, database, queue=queues[TRANSCRIPTION_QUEUE], segment_store=spool
    )._commit_conversation(closed)  # noqa: SLF001
    events = database.read_all(
        "SELECT payload_json FROM outbox_events WHERE queue_name=?", (ANALYSIS_QUEUE,)
    )
    assert len(events) == 1, "one analysis job per conversation"

    job = parse_analysis_job(str(events[0]["payload_json"]))
    assert sorted(job.campaign_ids) == sorted([campaign_a, campaign_b])
    for ref in job.matched_keywords:
        assert len(ref.campaign_ids) == 1


def test_one_keyword_owned_by_several_campaigns_keeps_every_owner(settings):
    """The multi-owner wire path, exercised where it is actually reachable.

    `station_keyword_bindings` is keyed on (station, keyword, campaign), so one
    keyword_id CAN be registered by several campaigns. The campaign API does not
    currently produce that shape, so it is built directly here to prove the
    contract carries every owner rather than collapsing to the first.
    """
    shared_keyword = "99999999-9999-4999-8999-999999999999"
    campaign_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    campaign_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    index = build_index(
        STATION,
        [
            binding(shared_keyword, campaign_a, "NVIDIA"),
            binding(shared_keyword, campaign_b, "NVIDIA"),
        ],
    )
    matches = KeywordMatcher(index).match("the NVIDIA launch").matches

    assert len(matches) == 1, "one keyword_id, one match"
    assert sorted(matches[0].campaign_ids) == sorted([campaign_a, campaign_b])

    ref = MatchedKeywordRef(
        keyword_id=matches[0].keyword_id,
        campaign_ids=list(matches[0].campaign_ids),
        canonical_value=matches[0].canonical_value,
        matched_text=matches[0].matched_text,
        match_level=matches[0].match_level,
        start_char=matches[0].start_char,
        end_char=matches[0].end_char,
        start_ms=0,
        end_ms=0,
        confidence=matches[0].confidence,
    )
    assert sorted(ref.campaign_ids) == sorted([campaign_a, campaign_b])
    assert ref.resolved_campaign_ids([]) == tuple(ref.campaign_ids)


def test_ownership_reaches_mention_campaigns_and_mention_keywords(
    settings, database, spool, queues, analysis_worker
):
    campaign_a = create_campaign(
        database, name="Chips Campaign", station_ids=[STATION], keywords=[("NVIDIA", "brand")]
    )
    campaign_b = create_campaign(
        database, name="Parts Campaign", station_ids=[STATION], keywords=[("GPU", "product")]
    )
    SubscriptionPlanner(settings, database).plan_once()
    matcher = KeywordMatcher(SubscriptionPlanner(settings, database).keyword_index_for(STATION))
    text = "the NVIDIA GPU launch"
    closed = close_conversation(settings, [segment(0, text, matcher.match(text).matches)])

    TranscriptionWorker(
        settings, database, queue=queues[TRANSCRIPTION_QUEUE], segment_store=spool
    )._commit_conversation(closed)  # noqa: SLF001
    OutboxDispatcher(database, queues).dispatch_once()
    assert analysis_worker.processor.poll_once(analysis_worker.handle)["processed"] == 1

    campaigns = {
        str(r["campaign_id"]) for r in database.read_all("SELECT campaign_id FROM mention_campaigns")
    }
    assert campaigns == {campaign_a, campaign_b}, "the conversation belongs to both"

    owners = {
        str(r["canonical_value"]): str(r["campaign_id"])
        for r in database.read_all("SELECT canonical_value, campaign_id FROM mention_keywords")
    }
    assert owners == {"NVIDIA": campaign_a, "GPU": campaign_b}

    assert len(database.read_all("SELECT 1 FROM mention_events")) == 1
    assert len(database.read_all("SELECT 1 FROM analysis_results")) == 1
