"""Segment encoding: WAV correctness and the Opus fallback contract."""
from __future__ import annotations

import shutil
import struct
import wave

import pytest

from app.pipeline.errors import ResourceExhaustedError
from app.services.segment_encoder import (
    SegmentEncodeError,
    SegmentEncoder,
    encode_opus,
    encode_wav,
)
from tests.fixtures import audio

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_wav_round_trips_the_exact_samples() -> None:
    pcm = audio.speech_like(0.5)
    encoded = encode_wav(pcm, sample_rate=16_000)

    import io

    with wave.open(io.BytesIO(encoded), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.readframes(handle.getnframes()) == pcm, "WAV must be lossless"


def test_encoder_falls_back_to_wav_when_ffmpeg_is_absent() -> None:
    encoder = SegmentEncoder(sample_rate=16_000, ffmpeg_binary="definitely-not-a-real-binary")
    data, extension = encoder.encode(audio.speech_like(0.5))
    assert extension == "wav"
    assert data.startswith(b"RIFF")
    assert encoder.opus_available is False
    assert encoder.extension == "wav"


def test_fallback_is_sticky_so_a_failed_spawn_is_paid_once() -> None:
    encoder = SegmentEncoder(sample_rate=16_000, ffmpeg_binary="definitely-not-a-real-binary")
    encoder.encode(audio.speech_like(0.2))
    assert encoder.opus_available is False
    # Second call must not attempt the subprocess again.
    _, extension = encoder.encode(audio.speech_like(0.2))
    assert extension == "wav"


def test_empty_segments_are_refused() -> None:
    encoder = SegmentEncoder(sample_rate=16_000)
    with pytest.raises(ResourceExhaustedError):
        encoder.encode(b"")


def test_missing_binary_raises_a_named_error() -> None:
    with pytest.raises(SegmentEncodeError, match="not installed"):
        encode_opus(b"\x00\x00" * 100, sample_rate=16_000, ffmpeg_binary="not-a-real-binary")


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed on this host")
def test_opus_is_substantially_smaller_than_raw_pcm() -> None:
    pcm = audio.speech_like(5.0)
    encoder = SegmentEncoder(sample_rate=16_000, bitrate="24k")
    data, extension = encoder.encode(pcm)
    if extension == "wav":  # libopus not compiled into this ffmpeg build
        pytest.skip("this ffmpeg build has no libopus encoder")
    assert data[:4] == b"OggS"
    assert len(data) < len(pcm) / 5, "24 kbit/s Opus should be far smaller than 256 kbit/s PCM"


def test_wav_header_declares_the_configured_sample_rate() -> None:
    encoded = encode_wav(b"\x00\x00" * 1000, sample_rate=8_000)
    # Bytes 24..28 of a canonical WAV header are the sample rate, little-endian.
    assert struct.unpack("<I", encoded[24:28])[0] == 8_000
