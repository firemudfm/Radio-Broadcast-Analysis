from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import CampaignCreate
from app.services.conversation import ConversationService
from app.services.semantic import SemanticDiscoveryService

# Broadcast fixtures sit one day in the past so they always fall inside the
# semantic scan lookback window regardless of when the suite runs.
_FIXTURE_DT = datetime.now(UTC) - timedelta(days=1)
FIXTURE_DAY = _FIXTURE_DT.strftime("%Y-%m-%d")
FIXTURE_DAY_PATH = _FIXTURE_DT.strftime("%Y/%m/%d")
FIXTURE_DAY_COMPACT = _FIXTURE_DT.strftime("%Y%m%d")


class FakeSemanticLlm:
    def match_keyword(self, **kwargs):
        assert kwargs["target"] == "Hello"
        assert "Hallo und herzlich willkommen" in kwargs["full_transcript"]
        return {
            "is_match": True,
            "match_type": "translated_equivalent",
            "matched_text": "Hallo",
            "target_relevance": "direct",
            "summary": "The speaker greets listeners.",
            "why_relevant": "Hallo is the German greeting equivalent.",
            "sentiment": "neutral",
            "confidence": 0.91,
            "needs_review": False,
            "error": None,
        }


class FakeStations:
    def station_map(self):
        return {
            "hertz879": {
                "id": "hertz879",
                "name": "Hertz 87.9",
                "country_code": "DE",
                "language_codes": ["de"],
            }
        }


def test_concept_keyword_semantic_match_creates_auditable_mention(settings, database, fake_s3):
    settings = settings.model_copy(
        update={
            "RADIO_SEMANTIC_SETTLE_SECONDS": 0,
            "RADIO_SEMANTIC_GROUPS_PER_CYCLE": 2,
        }
    )
    payload = CampaignCreate.model_validate(
        {
            "name": "Greeting watch",
            "keywords": [
                {
                    "value": "Hello",
                    "keyword_type": "concept",
                    "semantic_matching": True,
                    "semantic_threshold": 0.70,
                }
            ],
            "station_ids": ["hertz879"],
        }
    )
    database.create_campaign(payload, datetime.now(UTC) - timedelta(days=10))
    transcript_key = (
        f"transcripts/hertz879/{FIXTURE_DAY_PATH}/hertz879_{FIXTURE_DAY_COMPACT}T100000Z/"
        "segment_0001.transcript.json"
    )
    audio_key = f"clean-speech/hertz879/{FIXTURE_DAY_PATH}/chunk/segment.wav"
    fake_s3.put_bytes(audio_key, b"RIFF" + b"x" * 100)
    fake_s3.put_json(
        transcript_key,
        {
            "source_audio": f"s3://bucket/{audio_key}",
            "broadcast_start_utc": f"{FIXTURE_DAY}T10:00:00Z",
            "language": {"detected": "de", "probability": 0.99},
            "segments": [
                {
                    "id": 1,
                    "text": "Hallo und herzlich willkommen bei unserer Sendung.",
                    "broadcast_start_utc": f"{FIXTURE_DAY}T10:00:00Z",
                    "broadcast_end_utc": f"{FIXTURE_DAY}T10:00:04Z",
                    "words": [
                        {
                            "word": " Hallo",
                            "broadcast_start_utc": f"{FIXTURE_DAY}T10:00:00Z",
                            "broadcast_end_utc": f"{FIXTURE_DAY}T10:00:00.5Z",
                            "probability": 0.98,
                        },
                        {"word": " und"},
                        {"word": " herzlich"},
                        {"word": " willkommen"},
                        {"word": " bei"},
                        {"word": " unserer"},
                        {"word": " Sendung."},
                    ],
                }
            ],
        },
    )
    fake_s3.objects[transcript_key]["LastModified"] = datetime.now(UTC) - timedelta(minutes=5)
    service = SemanticDiscoveryService(
        settings,
        database,
        fake_s3,
        FakeStations(),
        ConversationService(settings, fake_s3),
        FakeSemanticLlm(),
    )

    stats = service.scan_once()

    assert stats["matches_created"] == 1
    mentions, total = database.list_mentions(limit=10, offset=0)
    assert total == 1
    assert mentions[0]["keyword"] == "Hello"
    assert mentions[0]["matched_alias"] == "Hallo"
    assert mentions[0]["detected_language"] == "de"
    audit_keys = [key for key in fake_s3.objects if key.startswith("results/semantic-matches/")]
    assert len(audit_keys) == 1


def test_brand_keyword_does_not_enable_semantic_matching_by_default():
    payload = CampaignCreate.model_validate(
        {
            "name": "Brand watch",
            "keywords": [{"value": "TechSara"}],
            "station_ids": ["hertz879"],
        }
    )
    assert payload.keywords[0].semantic_matching is False
    assert payload.keywords[0].keyword_type == "brand"


def test_concept_keyword_enables_semantic_matching_by_default():
    payload = CampaignCreate.model_validate(
        {
            "name": "Concept watch",
            "keywords": [{"value": "Hello", "keyword_type": "concept"}],
            "station_ids": ["hertz879"],
        }
    )
    assert payload.keywords[0].semantic_matching is True

class FailIfCalledLlm:
    def match_keyword(self, **kwargs):  # pragma: no cover - must not execute
        raise AssertionError(f"LLM should not be called for an exact alias: {kwargs}")


def test_exact_alias_backfill_does_not_call_llm(settings, database, fake_s3):
    settings = settings.model_copy(update={"RADIO_SEMANTIC_SETTLE_SECONDS": 0})
    payload = CampaignCreate.model_validate(
        {
            "name": "TechSara watch",
            "keywords": [{"value": "TechSara", "aliases": ["Tech Sara"]}],
            "station_ids": ["hertz879"],
            "backfill_days": 7,
        }
    )
    database.create_campaign(payload, datetime.now(UTC) - timedelta(days=10))
    transcript_key = (
        f"transcripts/hertz879/{FIXTURE_DAY_PATH}/hertz879_{FIXTURE_DAY_COMPACT}T110000Z/"
        "segment_0001.transcript.json"
    )
    audio_key = f"clean-speech/hertz879/{FIXTURE_DAY_PATH}/chunk2/segment.wav"
    fake_s3.put_bytes(audio_key, b"RIFF" + b"x" * 100)
    fake_s3.put_json(
        transcript_key,
        {
            "source_audio": f"s3://bucket/{audio_key}",
            "broadcast_start_utc": f"{FIXTURE_DAY}T11:00:00Z",
            "language": {"detected": "en", "probability": 0.99},
            "segments": [
                {
                    "id": 1,
                    "text": "Today Tech Sara announced a new product.",
                    "broadcast_start_utc": f"{FIXTURE_DAY}T11:00:00Z",
                    "broadcast_end_utc": f"{FIXTURE_DAY}T11:00:04Z",
                    "words": [
                        {"word": " Today"},
                        {
                            "word": " Tech",
                            "broadcast_start_utc": f"{FIXTURE_DAY}T11:00:00.5Z",
                            "broadcast_end_utc": f"{FIXTURE_DAY}T11:00:00.9Z",
                        },
                        {
                            "word": " Sara",
                            "broadcast_start_utc": f"{FIXTURE_DAY}T11:00:00.9Z",
                            "broadcast_end_utc": f"{FIXTURE_DAY}T11:00:01.3Z",
                        },
                        {"word": " announced"},
                        {"word": " a"},
                        {"word": " new"},
                        {"word": " product."},
                    ],
                }
            ],
        },
    )
    fake_s3.objects[transcript_key]["LastModified"] = datetime.now(UTC) - timedelta(minutes=5)
    service = SemanticDiscoveryService(
        settings,
        database,
        fake_s3,
        FakeStations(),
        ConversationService(settings, fake_s3),
        FailIfCalledLlm(),
    )

    stats = service.scan_once()

    assert stats["exact_matches"] == 1
    assert stats["semantic_matches"] == 0
    mentions, total = database.list_mentions(limit=10, offset=0)
    assert total == 1
    assert mentions[0]["matched_alias"] == "Tech Sara"


def test_token_exact_match_does_not_match_inside_a_larger_word():
    binding = {
        "display_name": "hello",
        "aliases": [],
        "match_mode": "tokens",
    }
    assert SemanticDiscoveryService._exact_match("shelloworld", binding) is None
    match = SemanticDiscoveryService._exact_match("She said hello, listeners.", binding)
    assert match is not None
    assert match["matched_text"] == "hello"


def test_substring_exact_match_can_match_inside_a_larger_word():
    binding = {
        "display_name": "hello",
        "aliases": [],
        "match_mode": "substring",
    }
    match = SemanticDiscoveryService._exact_match("shelloworld", binding)
    assert match is not None
    assert match["matched_text"] == "hello"



def test_semantic_match_supports_production_nested_source_audio(settings, database, fake_s3):
    settings = settings.model_copy(
        update={
            "RADIO_SEMANTIC_SETTLE_SECONDS": 0,
            "RADIO_SEMANTIC_GROUPS_PER_CYCLE": 2,
        }
    )
    payload = CampaignCreate.model_validate(
        {
            "name": "Nested source greeting watch",
            "keywords": [
                {
                    "value": "Hello",
                    "keyword_type": "concept",
                    "semantic_matching": True,
                    "semantic_threshold": 0.70,
                }
            ],
            "station_ids": ["hertz879"],
        }
    )
    database.create_campaign(payload, datetime.now(UTC) - timedelta(days=10))
    transcript_key = (
        f"transcripts/hertz879/{FIXTURE_DAY_PATH}/hertz879_{FIXTURE_DAY_COMPACT}T120000Z/"
        "segment_0001.transcript.json"
    )
    audio_key = f"clean-speech/hertz879/{FIXTURE_DAY_PATH}/chunk3/segment.wav"
    fake_s3.put_bytes(audio_key, b"RIFF" + b"x" * 100)
    fake_s3.put_json(
        transcript_key,
        {
            "source": {
                "audio_s3_uri": f"s3://bucket/{audio_key}",
                "filter_broadcast_start_utc": f"{FIXTURE_DAY}T12:00:00Z",
                "filter_broadcast_end_utc": f"{FIXTURE_DAY}T12:00:04Z",
            },
            "language": {"detected": "de", "probability": 0.99},
            "segments": [
                {
                    "id": 1,
                    "text": "Hallo und herzlich willkommen bei unserer Sendung.",
                    "broadcast_start_utc": f"{FIXTURE_DAY}T12:00:00Z",
                    "broadcast_end_utc": f"{FIXTURE_DAY}T12:00:04Z",
                    "words": [
                        {
                            "word": " Hallo",
                            "broadcast_start_utc": f"{FIXTURE_DAY}T12:00:00Z",
                            "broadcast_end_utc": f"{FIXTURE_DAY}T12:00:00.5Z",
                            "probability": 0.98,
                        },
                        {"word": " und"},
                        {"word": " herzlich"},
                        {"word": " willkommen"},
                        {"word": " bei"},
                        {"word": " unserer"},
                        {"word": " Sendung."},
                    ],
                }
            ],
        },
    )
    fake_s3.objects[transcript_key]["LastModified"] = datetime.now(UTC) - timedelta(minutes=5)
    service = SemanticDiscoveryService(
        settings,
        database,
        fake_s3,
        FakeStations(),
        ConversationService(settings, fake_s3),
        FakeSemanticLlm(),
    )

    stats = service.scan_once()

    assert stats["matches_created"] == 1
    assert stats["errors"] == 0
    mentions, total = database.list_mentions(limit=10, offset=0)
    assert total == 1
    assert mentions[0]["audio_available"] is True
    assert mentions[0]["matched_alias"] == "Hallo"
