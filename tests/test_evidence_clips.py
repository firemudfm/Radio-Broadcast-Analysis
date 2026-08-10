"""Pipeline mentions get playable audio clips.

The production gap: matched segments were retained in the spool forever, but
nothing cut a clip and attached it, so mention_events.evidence_available was
always 0 and the audio-token route answered 409 for every pipeline mention.

EvidenceClipService closes the gap; these tests prove the whole chain, from
retained spool bytes to a signed streaming URL.
"""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.pipeline.local_segment_store import LocalSegmentStore
from app.pipeline.segment_store import SegmentRef
from app.s3_utils import is_allowed_audio_key
from app.services.audio import AudioService
from app.services.evidence import EvidenceCaptureError, EvidenceClipService

NOW = datetime.now(UTC)
STAMP = NOW.isoformat()
STATION_ID = "rb-78012206-1aa1-11e9-a80b-52543be04c81"


class FakeRequest:
    def url_for(self, name: str, **values) -> str:
        assert name == "stream_audio"
        return f"http://127.0.0.1:8788/api/v1/brand-signal/audio/{values['token']}"


@pytest.fixture
def store(tmp_path) -> LocalSegmentStore:
    return LocalSegmentStore(tmp_path / "spool")


def seed_mention_with_segments(
    database,
    store: LocalSegmentStore,
    *,
    mention_id: str,
    segment_count: int = 1,
    stamp_conversation: bool = True,
    payloads: list[bytes] | None = None,
) -> list[bytes]:
    """A pipeline mention whose segments hold real, digest-valid spool bytes."""
    conversation_id = f"conv-{mention_id}"
    written: list[bytes] = []
    descriptors = []
    for index in range(segment_count):
        data = (
            payloads[index]
            if payloads is not None
            else f"segment-{index}-bytes".encode()
        )
        ref = SegmentRef(station_id=STATION_ID, segment_id=str(uuid4()))
        descriptors.append((ref, store.write(ref, data)))
        written.append(data)

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
            "INSERT INTO station_sessions(station_session_id, station_id,"
            " generation, shard_index, status, started_at_utc)"
            " VALUES (?,?,1,0,'streaming',?)",
            (f"sess-{mention_id}", STATION_ID, STAMP),
        )
        connection.execute(
            "INSERT INTO conversation_sessions(conversation_id, station_id,"
            " station_session_id, state, first_sequence_number,"
            " last_sequence_number, started_at_utc, transcript_text, trace_id,"
            " created_at_utc, updated_at_utc) VALUES (?,?,?,'closed',1,1,?,?,?,?,?)",
            (
                conversation_id, STATION_ID, f"sess-{mention_id}", STAMP,
                "Good night everyone.", "trace-1", STAMP, STAMP,
            ),
        )
        first_transcript_id = None
        for index, (ref, descriptor) in enumerate(descriptors):
            connection.execute(
                "INSERT INTO audio_segments(segment_id, station_id,"
                " station_session_id, sequence_number, started_at_utc,"
                " ended_at_utc, duration_ms, content_class, storage_backend,"
                " storage_path, sha256, size_bytes, disposition, trace_id,"
                " created_at_utc, updated_at_utc)"
                " VALUES (?,?,?,?,?,?,60000,'speech',?,?,?,?,'retained',"
                " 'trace-1',?,?)",
                (
                    ref.segment_id, STATION_ID, f"sess-{mention_id}", index + 1,
                    STAMP, STAMP, descriptor.backend, descriptor.path,
                    descriptor.sha256, descriptor.size_bytes, STAMP, STAMP,
                ),
            )
            transcript_id = f"t-{mention_id}-{index}"
            if first_transcript_id is None:
                first_transcript_id = transcript_id
            connection.execute(
                "INSERT INTO transcripts(transcript_id, segment_id, station_id,"
                " conversation_id, asr_pass, text, detected_language,"
                " model_name, created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    transcript_id, ref.segment_id, STATION_ID,
                    conversation_id if stamp_conversation else None,
                    "a", f"Part {index}.", "en", "faster-whisper", STAMP,
                ),
            )
        connection.execute(
            "INSERT INTO mention_events(mention_id, conversation_id, station_id,"
            " station_name, content_type, detected_language,"
            " broadcast_start_utc, broadcast_end_utc, transcript_id, trace_id,"
            " created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mention_id, conversation_id, STATION_ID, "MANGORADIO",
                "speech", "en", STAMP, STAMP, first_transcript_id, "trace-1",
                STAMP, STAMP,
            ),
        )
        connection.execute(
            "INSERT INTO mention_campaigns(mention_id, campaign_id, included,"
            " created_at_utc) VALUES (?,?,1,?)",
            (mention_id, "c1", STAMP),
        )
        connection.execute(
            "INSERT INTO mention_keywords(mention_id, keyword_id, campaign_id,"
            " canonical_value, matched_text, match_level, confirmed,"
            " created_at_utc) VALUES (?,?,?,?,?,?,1,?)",
            (mention_id, "k1", "c1", "night", "Night", "exact", STAMP),
        )

    database.write(write)
    return written


# =============================================================================
# The full chain: spool bytes -> S3 clip -> streamable mention audio
# =============================================================================


def test_capture_attaches_a_clip_and_the_audio_route_serves_it(
    settings, database, store, fake_s3
) -> None:
    mention_id = str(uuid4())
    payload = b"opus-bytes-for-the-clip"
    seed_mention_with_segments(
        database, store, mention_id=mention_id, payloads=[payload]
    )
    service = EvidenceClipService(settings, database, store, fake_s3)

    assert service.capture(mention_id) is True

    expected_key = f"evidence/{NOW:%Y/%m/%d}/{mention_id}.opus"
    stored = fake_s3.objects[expected_key]
    assert bytes(stored["Body"]) == payload
    assert stored["ContentType"] == "audio/ogg"

    view = database.mention_view_by_id(mention_id)
    assert view is not None and view["audio_available"] is True

    reference = database.mention_audio(mention_id)
    assert reference is not None and reference["audio_s3_key"] == expected_key

    audio = AudioService(settings, database, fake_s3)
    token_result = audio.create_token(mention_id, FakeRequest())
    assert "/api/v1/brand-signal/audio/" in token_result["url"]


def test_capture_is_idempotent(settings, database, store, fake_s3) -> None:
    mention_id = str(uuid4())
    seed_mention_with_segments(database, store, mention_id=mention_id)
    service = EvidenceClipService(settings, database, store, fake_s3)
    assert service.capture(mention_id) is True
    objects_after_first = dict(fake_s3.objects)
    assert service.capture(mention_id) is True
    assert fake_s3.objects == objects_after_first


def test_capture_resolves_segments_through_the_final_transcript(
    settings, database, store, fake_s3
) -> None:
    """Mentions from before close-stamping have conversation_id=NULL on their
    transcripts; the mention's own transcript_id still finds the audio."""
    mention_id = str(uuid4())
    seed_mention_with_segments(
        database, store, mention_id=mention_id, stamp_conversation=False
    )
    service = EvidenceClipService(settings, database, store, fake_s3)
    assert service.capture(mention_id) is True
    view = database.mention_view_by_id(mention_id)
    assert view is not None and view["audio_available"] is True


def fake_ffmpeg_writing(payload: bytes):
    """A stand-in for subprocess.run that writes the command's output file.

    FFmpeg is invoked with cwd=<workdir> and a bare output filename as the
    final argument; real muxing needs a seekable file (WAV rewrites its RIFF
    sizes on close), so the fake honours the same contract.
    """
    captured_commands: list[list[str]] = []

    def run(command, **kwargs):
        captured_commands.append(list(command))
        from pathlib import Path

        (Path(kwargs["cwd"]) / command[-1]).write_bytes(payload)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    return run, captured_commands


def test_multi_segment_clips_are_concatenated_losslessly(
    settings, database, store, fake_s3, monkeypatch
) -> None:
    mention_id = str(uuid4())
    seed_mention_with_segments(database, store, mention_id=mention_id, segment_count=3)

    fake_run, captured_commands = fake_ffmpeg_writing(b"CONCATENATED")
    monkeypatch.setattr("app.services.evidence.subprocess.run", fake_run)
    service = EvidenceClipService(settings, database, store, fake_s3)
    assert service.capture(mention_id) is True

    expected_key = f"evidence/{NOW:%Y/%m/%d}/{mention_id}.opus"
    assert bytes(fake_s3.objects[expected_key]["Body"]) == b"CONCATENATED"
    command = captured_commands[0]
    assert "concat" in command
    assert "copy" in command  # same-format segments are stream-copied, not re-encoded
    # A file, never a pipe: piped WAV output cannot rewrite its RIFF sizes and
    # would upload a permanently corrupt clip.
    assert command[-1] != "pipe:1"
    # Bare filenames resolved against cwd: no path ever needs concat quoting.
    assert "segments.txt" in command


def test_capture_without_segments_leaves_the_mention_clipless(
    settings, database, store, fake_s3
) -> None:
    mention_id = str(uuid4())
    seed_mention_with_segments(database, store, mention_id=mention_id)
    database.write(
        lambda connection: connection.execute(
            "UPDATE audio_segments SET disposition='deleted'"
        )
    )
    service = EvidenceClipService(settings, database, store, fake_s3)
    with pytest.raises(EvidenceCaptureError):
        service.capture(mention_id)
    assert not fake_s3.objects
    view = database.mention_view_by_id(mention_id)
    assert view is not None and view["audio_available"] is False


def test_the_transcript_fallback_refuses_a_partial_tail_clip(
    settings, database, store, fake_s3
) -> None:
    """Pre-stamping mentions resolve only the LAST segment. When the broadcast
    window is much longer than that segment, the clip would be the wrong
    audio; wrong evidence must be refused, not attached."""
    mention_id = str(uuid4())
    seed_mention_with_segments(
        database, store, mention_id=mention_id, stamp_conversation=False
    )
    # The seeded segment is 60s; stretch the broadcast window to 5 minutes.
    opened = NOW.replace(microsecond=0)
    closed = opened + timedelta(minutes=5)
    database.write(
        lambda connection: connection.execute(
            "UPDATE mention_events SET broadcast_start_utc=?, broadcast_end_utc=?"
            " WHERE mention_id=?",
            (
                opened.isoformat().replace("+00:00", "Z"),
                closed.isoformat().replace("+00:00", "Z"),
                mention_id,
            ),
        )
    )
    service = EvidenceClipService(settings, database, store, fake_s3)
    with pytest.raises(EvidenceCaptureError):
        service.capture(mention_id)
    assert not fake_s3.objects


# =============================================================================
# The backlog sweep must not starve behind failures
# =============================================================================


def test_backlog_sweep_reaches_mentions_behind_a_wall_of_failures(
    settings, database, store, fake_s3
) -> None:
    """Ten newest mentions that cannot be captured must not block older
    mentions from being backfilled: the sweep pages past known failures."""
    from app.workers.analysis import AnalysisWorker

    old_id = str(uuid4())
    seed_mention_with_segments(database, store, mention_id=old_id)
    database.write(
        lambda connection: connection.execute(
            "UPDATE mention_events SET created_at_utc='2026-01-01T00:00:00Z'"
            " WHERE mention_id=?",
            (old_id,),
        )
    )
    broken: list[str] = []
    for _ in range(10):
        mention_id = str(uuid4())
        seed_mention_with_segments(database, store, mention_id=mention_id)
        broken.append(mention_id)
    database.write(
        lambda connection: connection.execute(
            "UPDATE audio_segments SET disposition='deleted' WHERE segment_id IN ("
            " SELECT t.segment_id FROM transcripts t"
            " JOIN mention_events e ON e.conversation_id = t.conversation_id"
            f" WHERE e.mention_id IN ({','.join('?' for _ in broken)}))",
            tuple(broken),
        )
    )

    worker = AnalysisWorker(
        settings,
        database,
        queue=object(),
        analyzer=object(),
        result_writer=object(),
        s3_client=fake_s3,
        evidence_service=EvidenceClipService(settings, database, store, fake_s3),
    )
    worker._evidence_backlog()

    view = database.mention_view_by_id(old_id)
    assert view is not None and view["audio_available"] is True
    assert len(worker._evidence_failed) == 10


# =============================================================================
# The streaming allowlist
# =============================================================================


def test_evidence_keys_are_streamable_and_nothing_else_new_is() -> None:
    assert is_allowed_audio_key("evidence/2026/08/10/abc.opus") is True
    assert is_allowed_audio_key("clean-speech/hertz879/test.wav") is True
    assert is_allowed_audio_key("evidence/") is False
    assert is_allowed_audio_key("mentions/2026/08/10/abc/metadata.json") is False
    assert is_allowed_audio_key("temp-speech/rb-1/seg.opus") is False


def test_a_renamed_evidence_prefix_stays_streamable() -> None:
    """The allowlist follows RADIO_EVIDENCE_PREFIX instead of hardcoding it,
    so a renamed prefix cannot strand uploaded clips as unstreamable."""
    assert (
        is_allowed_audio_key(
            "brand-evidence/2026/08/10/abc.opus", evidence_prefix="brand-evidence/"
        )
        is True
    )
    assert is_allowed_audio_key("brand-evidence/2026/08/10/abc.opus") is False
