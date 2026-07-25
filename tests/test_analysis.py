from __future__ import annotations

from datetime import UTC, datetime

from app.models import CampaignCreate, MentionDetailView
from app.services.analysis import MentionAnalysisService
from app.services.conversation import ConversationService
from app.services.llm import LocalLlmClient


class FakeLlm(LocalLlmClient):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.calls = 0

    def analyze(self, **kwargs):
        self.calls += 1
        assert "complete transcript" in kwargs["full_transcript"]
        return {
            "status": "ready",
            "model": "fake-small-llm",
            "summary": "The speaker directly praises the target.",
            "why_relevant": "The target is explicitly named.",
            "speaker_intent": "endorsement",
            "sentiment": "positive",
            "target_relevance": "direct",
            "key_points": ["Explicit praise"],
            "evidence": ["Super Suckers"],
            "confidence": 0.91,
            "needs_review": False,
            "generated_at_utc": "2026-07-13T02:00:00Z",
            "error": None,
        }


def test_analysis_is_cached_and_detail_validates(settings, database, fake_s3) -> None:
    payload = CampaignCreate.model_validate(
        {"name": "Watch", "keywords": [{"value": "Supersuckers"}], "station_ids": ["hertz879"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(UTC))
    binding = database.active_bindings()[0]
    transcript_key = "transcripts/hertz879/2026/07/13/chunk/segment.transcript.json"
    fake_s3.put_json(
        transcript_key,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "This is the complete transcript about the Super Suckers.",
                    "broadcast_start_utc": "2026-07-13T01:00:00Z",
                    "broadcast_end_utc": "2026-07-13T01:00:05Z",
                    "words": [
                        {"word": " This"}, {"word": " is"}, {"word": " the"},
                        {"word": " complete"}, {"word": " transcript"}, {"word": " about"},
                        {"word": " the"},
                        {"word": " Super", "broadcast_start_utc": "2026-07-13T01:00:03Z", "broadcast_end_utc": "2026-07-13T01:00:03.4Z"},  # noqa: E501
                        {"word": " Suckers.", "broadcast_start_utc": "2026-07-13T01:00:03.4Z", "broadcast_end_utc": "2026-07-13T01:00:04Z"},  # noqa: E501
                    ],
                }
            ],
        },
    )
    mention_id = database.upsert_mention(
        {
            "campaign_id": campaign_id,
            "campaign_keyword_id": binding["keyword_id"],
            "station_id": "hertz879",
            "station_name": "Hertz 87.9",
            "station_country_code": "DE",
            "station_language_codes": ["de", "en"],
            "source_result_s3_key": "results/intelligence/test.json",
            "source_mention_id": "source-1",
            "entity_id": binding["entity_id"],
            "display_name": "Supersuckers",
            "matched_alias": "Super Suckers",
            "context": "Super Suckers.",
            "detected_language": "en",
            "language_probability": 0.99,
            "sentiment_label": "positive",
            "sentiment_score": 0.7,
            "sentiment_margin": 0.5,
            "needs_review": False,
            "broadcast_start_utc": "2026-07-13T01:00:03Z",
            "broadcast_end_utc": "2026-07-13T01:00:04Z",
            "audio_clip_start_utc": "2026-07-13T01:00:00Z",
            "audio_clip_end_utc": "2026-07-13T01:00:05Z",
            "audio_s3_key": "clean-speech/hertz879/test.wav",
            "raw_audio_s3_key": "raw-audio/hertz879/test.mp3",
            "transcript_s3_key": transcript_key,
        }
    )
    llm = FakeLlm(settings)
    service = MentionAnalysisService(
        settings,
        database,
        fake_s3,
        ConversationService(settings, fake_s3),
        llm,
    )
    first = service.detail(mention_id, refresh=True)
    assert first is not None
    validated = MentionDetailView.model_validate(first)
    assert validated.analysis.summary == "The speaker directly praises the target."
    assert validated.highlights[0].text.strip(" .") == "Super Suckers"
    assert llm.calls == 1
    second = service.detail(mention_id, refresh=False)
    assert second is not None
    assert llm.calls == 1
    assert database.analysis_counts()["ready"] == 1

    # A later transcript can extend the same contiguous conversation. The cached
    # LLM result is invalidated by the changed whole-transcript hash and queued
    # for the one shared worker instead of returning stale analysis.
    later_key = "transcripts/hertz879/2026/07/13/chunk2/segment.transcript.json"
    fake_s3.put_json(
        later_key,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "The host continues the same discussion.",
                    "broadcast_start_utc": "2026-07-13T01:00:10Z",
                    "broadcast_end_utc": "2026-07-13T01:00:15Z",
                }
            ],
        },
    )
    stale = service.detail(mention_id, refresh=False)
    assert stale is not None
    assert stale["analysis"]["status"] == "pending"
    assert "continues the same discussion" in stale["full_transcript"]
    assert llm.calls == 1
    refreshed = service.analyze(mention_id, force=False)
    assert refreshed is not None
    assert refreshed["analysis"]["status"] == "ready"
    assert llm.calls == 2


def test_detail_is_nonblocking_and_shared_worker_performs_analysis(settings, database, fake_s3) -> None:
    payload = CampaignCreate.model_validate(
        {"name": "Pending watch", "keywords": [{"value": "TechSara"}], "station_ids": ["hertz879"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(UTC))
    binding = database.active_bindings()[0]
    transcript_key = "transcripts/hertz879/2026/07/13/chunk2/segment.transcript.json"
    fake_s3.put_json(
        transcript_key,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "This is the complete transcript about TechSara.",
                    "broadcast_start_utc": "2026-07-13T02:00:00Z",
                    "broadcast_end_utc": "2026-07-13T02:00:05Z",
                    "words": [
                        {"word": " This"}, {"word": " is"}, {"word": " the"},
                        {"word": " complete"}, {"word": " transcript"}, {"word": " about"},
                        {
                            "word": " TechSara.",
                            "broadcast_start_utc": "2026-07-13T02:00:03Z",
                            "broadcast_end_utc": "2026-07-13T02:00:04Z",
                        },
                    ],
                }
            ],
        },
    )
    mention_id = database.upsert_mention(
        {
            "campaign_id": campaign_id,
            "campaign_keyword_id": binding["keyword_id"],
            "station_id": "hertz879",
            "station_name": "Hertz 87.9",
            "station_country_code": "DE",
            "station_language_codes": ["de", "en"],
            "source_result_s3_key": "results/intelligence/pending.json",
            "source_mention_id": "pending-source",
            "entity_id": binding["entity_id"],
            "display_name": "TechSara",
            "matched_alias": "TechSara",
            "context": "TechSara.",
            "detected_language": "en",
            "language_probability": 0.99,
            "sentiment_label": "neutral",
            "sentiment_score": None,
            "sentiment_margin": None,
            "needs_review": True,
            "broadcast_start_utc": "2026-07-13T02:00:03Z",
            "broadcast_end_utc": "2026-07-13T02:00:04Z",
            "audio_clip_start_utc": "2026-07-13T02:00:00Z",
            "audio_clip_end_utc": "2026-07-13T02:00:05Z",
            "audio_s3_key": "clean-speech/hertz879/pending.wav",
            "raw_audio_s3_key": "raw-audio/hertz879/pending.mp3",
            "transcript_s3_key": transcript_key,
        }
    )
    llm = FakeLlm(settings)
    service = MentionAnalysisService(
        settings,
        database,
        fake_s3,
        ConversationService(settings, fake_s3),
        llm,
    )

    detail = service.detail(mention_id)
    assert detail is not None
    assert detail["analysis"]["status"] == "pending"
    assert llm.calls == 0

    analyzed = service.analyze(mention_id)
    assert analyzed is not None
    assert analyzed["analysis"]["status"] == "ready"
    assert llm.calls == 1
