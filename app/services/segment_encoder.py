"""Encode PCM windows into the on-disk segment format (ADR-002 §4).

Choice of format
----------------
Segments are transient: written by the listener, read once by the transcription
worker, then deleted. What matters is (a) not damaging ASR accuracy and (b) not
filling the spool.

* **WAV/PCM** is the reference format. Lossless, zero dependencies, exactly what
  the decoder produced. Also ~320 kbit/s at 16 kHz mono, which is ~2.4 MB per
  minute per station -- fine transiently, expensive under backpressure.
* **Opus at 24 kbit/s mono, 16 kHz** is the default. Opus was designed for
  speech at low rates and is transparent enough for ASR well below this
  bitrate; it is ~13x smaller than raw PCM. Below roughly 16 kbit/s intelligible
  speech starts to degrade, so 24k is a deliberately conservative floor rather
  than the smallest workable number.

Lossy settings that measurably hurt ASR are not acceptable here, which is why
the bitrate is a validated setting with a safe default rather than something
tuned for disk.

Atomicity is the caller's concern (:mod:`app.pipeline.local_segment_store`
writes to a temporary name, fsyncs and renames). This module only produces
bytes, which keeps it synchronous, pure and trivially testable.
"""
from __future__ import annotations

import io
import logging
import subprocess  # nosec B404 - fixed binary, argument arrays, never shell=True
import wave

from ..pipeline.errors import ResourceExhaustedError

logger = logging.getLogger(__name__)

#: Hard ceiling on encoder runtime. A hung FFmpeg must not wedge a station.
ENCODE_TIMEOUT_SECONDS = 30.0


class SegmentEncodeError(RuntimeError):
    """Encoding failed in a way the caller must handle."""


def encode_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM in a WAV container. Stdlib only, cannot fail on this host."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def encode_opus(
    pcm: bytes,
    *,
    sample_rate: int,
    bitrate: str = "24k",
    channels: int = 1,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float = ENCODE_TIMEOUT_SECONDS,
) -> bytes:
    """Encode PCM to Ogg/Opus via FFmpeg.

    The argument list is explicit and never passed through a shell. Every value
    that could come from configuration (``ffmpeg_binary``, ``bitrate``) is
    already pattern-validated in :mod:`app.config`, so nothing here can become
    an argument-injection vector.
    """
    command = [
        ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-i", "pipe:0",
        "-c:a", "libopus",
        "-b:a", bitrate,
        # VOIP mode is tuned for speech intelligibility rather than music
        # fidelity, which is exactly the trade this pipeline wants.
        "-application", "voip",
        "-vn",
        "-f", "ogg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, validated args, no shell
            command,
            input=pcm,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise SegmentEncodeError(f"{ffmpeg_binary!r} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise SegmentEncodeError("Opus encoding timed out") from error

    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", "replace")[:300]
        raise SegmentEncodeError(f"FFmpeg exited {completed.returncode}: {detail}")
    return completed.stdout


class SegmentEncoder:
    """Encodes to Opus, falling back to WAV once FFmpeg proves unusable.

    The fallback is sticky rather than per-call: if libopus is missing, every
    segment would otherwise pay a failed subprocess spawn before falling back,
    which is a real cost at broadcast rates. The chosen extension travels with
    the segment in its ``StorageDescriptor``, so a mixed spool stays readable.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        bitrate: str = "24k",
        ffmpeg_binary: str = "ffmpeg",
        prefer_opus: bool = True,
    ) -> None:
        self._sample_rate = sample_rate
        self._bitrate = bitrate
        self._ffmpeg_binary = ffmpeg_binary
        self._opus_available = prefer_opus
        self._warned = False

    @property
    def extension(self) -> str:
        return "opus" if self._opus_available else "wav"

    @property
    def opus_available(self) -> bool:
        return self._opus_available

    def encode(self, pcm: bytes) -> tuple[bytes, str]:
        """Return ``(encoded_bytes, extension)`` for one segment."""
        if not pcm:
            raise ResourceExhaustedError("Refusing to encode an empty segment")
        if self._opus_available:
            try:
                return (
                    encode_opus(
                        pcm,
                        sample_rate=self._sample_rate,
                        bitrate=self._bitrate,
                        ffmpeg_binary=self._ffmpeg_binary,
                    ),
                    "opus",
                )
            except SegmentEncodeError as error:
                self._opus_available = False
                if not self._warned:
                    self._warned = True
                    # WARNING, not ERROR: capture continues correctly, it just
                    # costs more disk. An operator should see it once.
                    logger.warning(
                        "Opus encoding unavailable, falling back to WAV segments: %s", error
                    )
        return encode_wav(pcm, sample_rate=self._sample_rate), "wav"


__all__ = [
    "ENCODE_TIMEOUT_SECONDS",
    "SegmentEncodeError",
    "SegmentEncoder",
    "encode_opus",
    "encode_wav",
]
