from __future__ import annotations

from app.services.conversation import ConversationService, find_normalized_span


def test_normalized_span_handles_punctuation_and_spacing() -> None:
    text = "The Super-Suckers are on air."
    assert find_normalized_span(text, "Super Suckers") == (4, 17)


def test_full_chunk_transcript_and_timestamp_highlight(settings, fake_s3) -> None:
    key1 = (
        "transcripts/hertz879/2026/07/13/hertz879_20260713T013500Z/"
        "segment_0001.transcript.json"
    )
    key2 = (
        "transcripts/hertz879/2026/07/13/hertz879_20260713T013500Z/"
        "segment_0002.transcript.json"
    )
    fake_s3.put_json(
        key1,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "The presenter introduces the show.",
                    "broadcast_start_utc": "2026-07-13T01:36:00Z",
                    "broadcast_end_utc": "2026-07-13T01:36:03Z",
                    "words": [
                        {
                            "word": " The",
                            "broadcast_start_utc": "2026-07-13T01:36:00Z",
                            "broadcast_end_utc": "2026-07-13T01:36:00.2Z",
                            "probability": 0.9,
                        },
                        {
                            "word": " presenter",
                            "broadcast_start_utc": "2026-07-13T01:36:00.2Z",
                            "broadcast_end_utc": "2026-07-13T01:36:01Z",
                            "probability": 0.9,
                        },
                        {"word": " introduces"},
                        {"word": " the"},
                        {"word": " show."},
                    ],
                }
            ],
        },
    )
    fake_s3.put_json(
        key2,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 2,
                    "text": "The Super Suckers are the greatest rock band in the world.",
                    "broadcast_start_utc": "2026-07-13T01:36:20Z",
                    "broadcast_end_utc": "2026-07-13T01:36:28Z",
                    "words": [
                        {"word": " The", "broadcast_start_utc": "2026-07-13T01:36:20Z", "broadcast_end_utc": "2026-07-13T01:36:20.2Z"},  # noqa: E501
                        {"word": " Super", "broadcast_start_utc": "2026-07-13T01:36:22Z", "broadcast_end_utc": "2026-07-13T01:36:22.4Z"},  # noqa: E501
                        {"word": " Suckers", "broadcast_start_utc": "2026-07-13T01:36:22.4Z", "broadcast_end_utc": "2026-07-13T01:36:23Z"},  # noqa: E501
                        {"word": " are"},
                        {"word": " the"},
                        {"word": " greatest"},
                        {"word": " rock"},
                        {"word": " band"},
                        {"word": " in"},
                        {"word": " the"},
                        {"word": " world."},
                    ],
                }
            ],
        },
    )
    service = ConversationService(settings, fake_s3)
    result = service.build(
        {
            "transcript_s3_key": key2,
            "keyword_value": "Supersuckers",
            "matched_alias": "Super Suckers",
            "display_name": "Supersuckers",
            "broadcast_start_utc": "2026-07-13T01:36:22Z",
            "broadcast_end_utc": "2026-07-13T01:36:23Z",
            "detected_language": "en",
        }
    )
    assert "presenter introduces the show" in result["full_transcript"]
    assert "Super Suckers are the greatest" in result["full_transcript"]
    assert len(result["transcript_source_keys"]) == 2
    assert result["highlights"][0]["text"].strip() == "Super Suckers"
    assert result["highlights"][0]["method"] == "timestamp"
    assert "Super Suckers" in result["highlighted_sentence"]


def test_dynamic_session_spans_neighboring_chunks_and_stops_at_real_speech_gap(settings, fake_s3) -> None:
    settings = settings.model_copy(
        update={
            "RADIO_CONVERSATION_SCAN_CHUNKS": 4,
            "RADIO_CONVERSATION_SESSION_GAP_SECONDS": 30.0,
            "RADIO_CONVERSATION_MAX_DURATION_SECONDS": 1800,
        }
    )
    prefix = "transcripts/hertz879/2026/07/13"
    previous = f"{prefix}/hertz879_20260713T100000Z/segment_0001.transcript.json"
    anchor = f"{prefix}/hertz879_20260713T100500Z/segment_0001.transcript.json"
    following = f"{prefix}/hertz879_20260713T101000Z/segment_0001.transcript.json"
    unrelated = f"{prefix}/hertz879_20260713T101500Z/segment_0001.transcript.json"

    fake_s3.put_json(
        previous,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "The interview begins with an introduction.",
                    "broadcast_start_utc": "2026-07-13T10:00:00Z",
                    "broadcast_end_utc": "2026-07-13T10:00:10Z",
                }
            ],
        },
    )
    fake_s3.put_json(
        anchor,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "The guest explains how TechSara built the product.",
                    "broadcast_start_utc": "2026-07-13T10:00:20Z",
                    "broadcast_end_utc": "2026-07-13T10:00:30Z",
                    "words": [
                        {"word": " The"},
                        {"word": " guest"},
                        {"word": " explains"},
                        {"word": " how"},
                        {
                            "word": " TechSara",
                            "broadcast_start_utc": "2026-07-13T10:00:24Z",
                            "broadcast_end_utc": "2026-07-13T10:00:25Z",
                        },
                        {"word": " built"},
                        {"word": " the"},
                        {"word": " product."},
                    ],
                }
            ],
        },
    )
    fake_s3.put_json(
        following,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "The host asks a follow-up question about the launch.",
                    "broadcast_start_utc": "2026-07-13T10:00:40Z",
                    "broadcast_end_utc": "2026-07-13T10:00:50Z",
                }
            ],
        },
    )
    fake_s3.put_json(
        unrelated,
        {
            "language": {"detected": "en"},
            "segments": [
                {
                    "id": 1,
                    "text": "A separate news bulletin begins.",
                    "broadcast_start_utc": "2026-07-13T10:02:00Z",
                    "broadcast_end_utc": "2026-07-13T10:02:05Z",
                }
            ],
        },
    )

    result = ConversationService(settings, fake_s3).build(
        {
            "transcript_s3_key": anchor,
            "keyword_value": "TechSara",
            "matched_alias": "TechSara",
            "display_name": "TechSara",
            "broadcast_start_utc": "2026-07-13T10:00:24Z",
            "broadcast_end_utc": "2026-07-13T10:00:25Z",
        }
    )

    assert "interview begins" in result["full_transcript"]
    assert "TechSara built the product" in result["full_transcript"]
    assert "follow-up question" in result["full_transcript"]
    assert "separate news bulletin" not in result["full_transcript"]
    assert result["transcript_source_keys"] == [previous, anchor, following]
    assert result["highlights"][0]["text"] == "TechSara"
    assert "TechSara" in result["highlighted_sentence"]


def test_source_group_scope_keeps_all_speech_clips_for_keyword_discovery(settings, fake_s3) -> None:
    prefix = "transcripts/hertz879/2026/07/13/hertz879_20260713T120000Z"
    first = f"{prefix}/segment_0001.transcript.json"
    later = f"{prefix}/segment_0002.transcript.json"
    fake_s3.put_json(
        first,
        {
            "segments": [
                {
                    "id": 1,
                    "text": "A short station introduction.",
                    "broadcast_start_utc": "2026-07-13T12:00:00Z",
                    "broadcast_end_utc": "2026-07-13T12:00:05Z",
                }
            ]
        },
    )
    fake_s3.put_json(
        later,
        {
            "segments": [
                {
                    "id": 1,
                    "text": "Much later in the chunk, TechSara is discussed.",
                    "broadcast_start_utc": "2026-07-13T12:03:00Z",
                    "broadcast_end_utc": "2026-07-13T12:03:06Z",
                }
            ]
        },
    )

    session = ConversationService(settings, fake_s3).build(
        {"transcript_s3_key": first, "display_name": "TechSara"}
    )
    assert "TechSara" not in session["full_transcript"]

    source_group = ConversationService(settings, fake_s3).build(
        {
            "transcript_s3_key": first,
            "conversation_scope": "source_group",
            "display_name": "TechSara",
        }
    )
    assert "short station introduction" in source_group["full_transcript"]
    assert "TechSara is discussed" in source_group["full_transcript"]
    assert source_group["transcript_source_keys"] == [first, later]
