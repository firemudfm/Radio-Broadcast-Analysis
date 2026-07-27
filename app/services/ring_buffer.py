"""Bounded per-station PCM ring buffer.

Radio is an infinite stream, so the only safe buffer is one that cannot grow.
This is a fixed-size ``bytearray`` allocated once at construction; appending
past the end overwrites the oldest audio rather than allocating. Memory per
station is therefore a constant known before the process starts:

    60 s x 16 000 Hz x 2 bytes = 1 920 000 bytes  (~1.83 MiB)

which is what makes ``RADIO_MAX_ACTIVE_UNIQUE_STATIONS`` a number an operator
can reason about (ADR-008).

Timestamps are derived from the **sample count**, not from ``datetime.now()``.
A live stream delivers audio in bursts, so wall-clock arrival time is jittery
and occasionally out of order; sample offsets are exact and monotonic, which is
what evidence timestamps and conversation ordering need. Wall-clock drift is
still recorded, because sustained drift is how you detect a stalling encoder.

Nothing here decides what to keep. Audio that passes through the buffer and is
never selected is simply overwritten -- ordinary music is never written to disk
merely because it was buffered (ADR-010).
"""
from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: 16-bit signed PCM. The pipeline decodes everything to this before analysis.
SAMPLE_WIDTH_BYTES = 2


@dataclass(frozen=True)
class PcmWindow:
    """A contiguous stretch of PCM lifted out of the buffer.

    ``start_offset_ms`` is measured from the start of the current *generation*
    (see :meth:`RingBuffer.reset`), not from process start, so it resets with
    the stream and never silently spans a reconnect.
    """

    pcm: bytes
    sample_rate: int
    start_offset_ms: int
    started_at: datetime
    ended_at: datetime
    generation: int
    truncated: bool = False

    @property
    def duration_ms(self) -> int:
        if self.sample_rate <= 0:
            return 0
        samples = len(self.pcm) // SAMPLE_WIDTH_BYTES
        return round(samples * 1000 / self.sample_rate)

    @property
    def end_offset_ms(self) -> int:
        return self.start_offset_ms + self.duration_ms

    @property
    def is_empty(self) -> bool:
        return not self.pcm

    def samples(self) -> array:
        """Decode to native-endian int16 for analysis.

        ``array`` decoding is C-speed stdlib, which matters: this runs on every
        frame of every active station.
        """
        values = array("h")
        values.frombytes(self.pcm[: len(self.pcm) - (len(self.pcm) % SAMPLE_WIDTH_BYTES)])
        if sys.byteorder == "big":
            # FFmpeg is asked for s16le, so on a big-endian host the bytes must
            # be swapped before they mean anything.
            values.byteswap()
        return values


class RingBufferError(RuntimeError):
    """The buffer was asked for audio it cannot supply."""


class RingBuffer:
    """Fixed-capacity circular PCM buffer for one station session."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        seconds: int = 60,
        started_at: datetime | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self._sample_rate = sample_rate
        self._seconds = seconds
        self._capacity = sample_rate * seconds * SAMPLE_WIDTH_BYTES
        self._buffer = bytearray(self._capacity)
        self._write = 0
        self._total = 0
        self._generation = 1
        self._generation_started_at = started_at or datetime.now(UTC)
        self._gap_ms = 0
        self._last_append_at: datetime | None = None

    # -- geometry -------------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @property
    def capacity_seconds(self) -> int:
        return self._seconds

    @property
    def generation(self) -> int:
        """Increments on every reset. Segments never span two generations."""
        return self._generation

    @property
    def generation_started_at(self) -> datetime:
        return self._generation_started_at

    @property
    def total_bytes(self) -> int:
        """Bytes written since the last reset, including overwritten ones."""
        return self._total

    @property
    def available_bytes(self) -> int:
        return min(self._total, self._capacity)

    @property
    def available_ms(self) -> int:
        return self._bytes_to_ms(self.available_bytes)

    @property
    def position_ms(self) -> int:
        """Offset of the write head from the start of this generation."""
        return self._bytes_to_ms(self._total)

    @property
    def oldest_offset_ms(self) -> int:
        """Offset of the oldest sample still held."""
        return self._bytes_to_ms(max(0, self._total - self._capacity))

    @property
    def gap_ms(self) -> int:
        """Total audio known to be missing in this generation."""
        return self._gap_ms

    # -- writing --------------------------------------------------------------

    def append(self, pcm: bytes, *, arrived_at: datetime | None = None) -> int:
        """Append PCM, overwriting the oldest audio once full.

        Returns the offset (ms from generation start) at which the data begins,
        so a caller can address it later without tracking its own cursor.
        """
        if not pcm:
            return self.position_ms
        remainder = len(pcm) % SAMPLE_WIDTH_BYTES
        if remainder:
            # A partial sample would shift every subsequent sample by one byte
            # and turn the whole stream into noise. Refuse rather than corrupt.
            raise RingBufferError(
                f"PCM length {len(pcm)} is not a whole number of {SAMPLE_WIDTH_BYTES}-byte samples"
            )

        start_offset_ms = self.position_ms
        data = memoryview(pcm)
        new_total = self._total + len(pcm)
        if len(data) > self._capacity:
            # A single write larger than the buffer: only the tail can survive.
            data = data[len(data) - self._capacity :]

        # The whole read path depends on one invariant: the byte at absolute
        # offset A lives at physical index ``A % capacity``. So the write
        # position is *derived* from the absolute offset of the retained data
        # rather than tracked separately -- an oversized write that reset the
        # cursor to zero would silently violate it and return misaligned audio.
        write_at = (new_total - len(data)) % self._capacity
        end = write_at + len(data)
        if end <= self._capacity:
            self._buffer[write_at:end] = data
        else:
            head = self._capacity - write_at
            self._buffer[write_at:] = data[:head]
            self._buffer[: len(data) - head] = data[head:]

        self._total = new_total
        self._write = new_total % self._capacity
        self._last_append_at = arrived_at or datetime.now(UTC)
        return start_offset_ms

    def mark_gap(self, duration_ms: int) -> None:
        """Record known-missing audio without fabricating samples.

        Silence is *not* inserted: padding a gap would make a dropout look like
        a pause in speech, which is exactly the signal the conversation
        assembler uses to decide a conversation ended.
        """
        if duration_ms > 0:
            self._gap_ms += int(duration_ms)

    # -- reading --------------------------------------------------------------

    def tail(self, seconds: float) -> PcmWindow:
        """The most recent ``seconds`` of audio, or everything held if less."""
        wanted = self._ms_to_bytes(int(round(max(0.0, seconds) * 1000)))
        size = min(wanted, self.available_bytes)
        start = self._total - size
        return self._window(start, self._total, truncated=size < wanted)

    def window(self, start_offset_ms: int, end_offset_ms: int) -> PcmWindow:
        """Audio between two generation-relative offsets, clamped to what is held.

        Clamping is explicit and flagged via ``PcmWindow.truncated`` rather than
        raising: a pre-roll request that reaches back further than the buffer
        holds should return what exists, and the caller needs to know it was
        short so the evidence boundary can be recorded honestly.
        """
        if end_offset_ms <= start_offset_ms:
            return self._window(self._total, self._total, truncated=False)
        requested_start = self._ms_to_bytes(max(0, start_offset_ms))
        requested_end = self._ms_to_bytes(max(0, end_offset_ms))
        floor = max(0, self._total - self._capacity)
        start = max(requested_start, floor)
        end = min(requested_end, self._total)
        if end <= start:
            return self._window(self._total, self._total, truncated=True)
        truncated = start > requested_start or end < requested_end
        return self._window(start, end, truncated=truncated)

    def _window(self, start: int, end: int, *, truncated: bool) -> PcmWindow:
        start_offset_ms = self._bytes_to_ms(start)
        pcm = self._read_absolute(start, end)
        started_at = self._generation_started_at + timedelta(milliseconds=start_offset_ms)
        ended_at = self._generation_started_at + timedelta(milliseconds=self._bytes_to_ms(end))
        return PcmWindow(
            pcm=pcm,
            sample_rate=self._sample_rate,
            start_offset_ms=start_offset_ms,
            started_at=started_at,
            ended_at=ended_at,
            generation=self._generation,
            truncated=truncated,
        )

    def _read_absolute(self, start: int, end: int) -> bytes:
        """Read by absolute byte offset since reset, resolving the wrap."""
        if end <= start:
            return b""
        floor = max(0, self._total - self._capacity)
        if start < floor or end > self._total:
            raise RingBufferError(
                f"Requested bytes [{start}, {end}) fall outside the retained "
                f"range [{floor}, {self._total})"
            )
        length = end - start
        begin = start % self._capacity
        finish = begin + length
        if finish <= self._capacity:
            return bytes(self._buffer[begin:finish])
        head = self._capacity - begin
        return bytes(self._buffer[begin:]) + bytes(self._buffer[: length - head])

    # -- lifecycle ------------------------------------------------------------

    def reset(self, *, started_at: datetime | None = None) -> int:
        """Start a new generation after a reconnect.

        The buffer is emptied rather than carried over: audio from before a
        disconnect and audio from after it are not contiguous, and splicing them
        would invent a conversation that never happened. Returns the new
        generation number.
        """
        self._write = 0
        self._total = 0
        self._gap_ms = 0
        self._generation += 1
        self._generation_started_at = started_at or datetime.now(UTC)
        self._last_append_at = None
        return self._generation

    def clear(self) -> None:
        """Release the audio when a station stops.

        The backing ``bytearray`` is replaced with an empty one so the memory is
        actually returned; zeroing in place would keep every stopped station's
        1.8 MiB resident for the life of the process.
        """
        self._buffer = bytearray(0)
        self._capacity = 0
        self._write = 0
        self._total = 0
        self._gap_ms = 0
        self._last_append_at = None

    def reopen(self, *, started_at: datetime | None = None) -> None:
        """Re-allocate after :meth:`clear`, for a station that restarts."""
        self._capacity = self._sample_rate * self._seconds * SAMPLE_WIDTH_BYTES
        self._buffer = bytearray(self._capacity)
        self.reset(started_at=started_at)

    # -- diagnostics ----------------------------------------------------------

    def drift_seconds(self, *, now: datetime | None = None) -> float | None:
        """Wall-clock minus stream-clock, in seconds.

        Persistent positive drift means audio is arriving slower than real time
        -- a stalling source or a saturated host -- which is invisible if you
        only ever look at sample offsets.
        """
        if self._last_append_at is None:
            return None
        elapsed = ((now or datetime.now(UTC)) - self._generation_started_at).total_seconds()
        return round(elapsed - (self.position_ms / 1000.0), 3)

    def stats(self) -> dict[str, float | int | None]:
        return {
            "generation": self._generation,
            "capacity_bytes": self._capacity,
            "available_ms": self.available_ms,
            "position_ms": self.position_ms,
            "oldest_offset_ms": self.oldest_offset_ms,
            "gap_ms": self._gap_ms,
            "drift_seconds": self.drift_seconds(),
        }

    # -- conversions ----------------------------------------------------------

    def _ms_to_bytes(self, milliseconds: int) -> int:
        samples = int(milliseconds) * self._sample_rate // 1000
        return samples * SAMPLE_WIDTH_BYTES

    def _bytes_to_ms(self, size: int) -> int:
        if self._sample_rate <= 0:
            return 0
        return round((size // SAMPLE_WIDTH_BYTES) * 1000 / self._sample_rate)


def bytes_per_second(sample_rate: int) -> int:
    return sample_rate * SAMPLE_WIDTH_BYTES


__all__ = [
    "SAMPLE_WIDTH_BYTES",
    "PcmWindow",
    "RingBuffer",
    "RingBufferError",
    "bytes_per_second",
]
