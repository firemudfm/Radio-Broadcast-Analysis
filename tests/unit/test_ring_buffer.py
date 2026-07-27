"""Ring-buffer behaviour: bounds, wrap correctness, generations and gaps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.ring_buffer import (
    SAMPLE_WIDTH_BYTES,
    RingBuffer,
    RingBufferError,
    bytes_per_second,
)

START = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def pcm(sample_count: int, value: int = 1000) -> bytes:
    return (value.to_bytes(2, "little", signed=True)) * sample_count


def ramp(sample_count: int, start: int = 0) -> bytes:
    """Distinct value per sample so wrap errors are detectable, not plausible."""
    return b"".join(
        ((start + index) % 30000).to_bytes(2, "little", signed=True)
        for index in range(sample_count)
    )


@pytest.fixture
def buffer() -> RingBuffer:
    return RingBuffer(sample_rate=1000, seconds=2, started_at=START)


def test_capacity_is_fixed_and_derived_from_configuration(buffer: RingBuffer) -> None:
    assert buffer.capacity_bytes == 1000 * 2 * SAMPLE_WIDTH_BYTES
    assert bytes_per_second(16_000) == 32_000


def test_memory_does_not_grow_with_input(buffer: RingBuffer) -> None:
    for _ in range(50):
        buffer.append(pcm(1000))
    # 50 seconds of audio pushed through a 2-second buffer.
    assert len(buffer._buffer) == buffer.capacity_bytes  # noqa: SLF001 - the invariant under test
    assert buffer.available_bytes == buffer.capacity_bytes
    assert buffer.total_bytes == 50 * 1000 * SAMPLE_WIDTH_BYTES


def test_tail_returns_the_most_recent_audio_across_a_wrap(buffer: RingBuffer) -> None:
    buffer.append(ramp(1500))
    buffer.append(ramp(1000, start=1500))
    window = buffer.tail(1.0)
    assert window.duration_ms == 1000
    assert window.pcm == ramp(1000, start=1500)


def test_window_by_absolute_offset(buffer: RingBuffer) -> None:
    buffer.append(ramp(2000))
    window = buffer.window(500, 1500)
    assert window.pcm == ramp(1000, start=500)
    assert window.start_offset_ms == 500
    assert window.started_at == START + timedelta(milliseconds=500)
    assert not window.truncated


def test_window_reaching_past_the_retained_range_is_clamped_and_flagged(
    buffer: RingBuffer,
) -> None:
    buffer.append(ramp(3000))  # 3s into a 2s buffer: the first second is gone.
    window = buffer.window(0, 2000)
    assert window.truncated, "a caller must be able to tell the pre-roll was short"
    assert window.start_offset_ms == 1000
    assert window.pcm == ramp(1000, start=1000)

    # A range entirely outside the retained window yields nothing, still flagged.
    evicted = buffer.window(0, 500)
    assert evicted.is_empty
    assert evicted.truncated


def test_a_write_larger_than_the_buffer_keeps_only_the_tail(buffer: RingBuffer) -> None:
    buffer.append(ramp(5000))
    window = buffer.tail(2.0)
    assert window.pcm == ramp(2000, start=3000)


def test_partial_samples_are_refused_rather_than_desynchronising(buffer: RingBuffer) -> None:
    with pytest.raises(RingBufferError):
        buffer.append(b"\x01\x02\x03")


def test_reset_starts_a_new_generation_and_drops_prior_audio(buffer: RingBuffer) -> None:
    buffer.append(ramp(1000))
    generation = buffer.reset(started_at=START + timedelta(seconds=30))
    assert generation == 2
    assert buffer.available_bytes == 0
    assert buffer.position_ms == 0
    assert buffer.generation_started_at == START + timedelta(seconds=30)
    # Audio from before a reconnect must not be spliced onto audio from after.
    buffer.append(ramp(500))
    assert buffer.tail(5.0).pcm == ramp(500)


def test_clear_releases_memory_and_reopen_restores_it(buffer: RingBuffer) -> None:
    buffer.append(pcm(1000))
    buffer.clear()
    assert len(buffer._buffer) == 0  # noqa: SLF001 - the invariant under test
    buffer.reopen(started_at=START)
    assert buffer.capacity_bytes == 1000 * 2 * SAMPLE_WIDTH_BYTES
    buffer.append(pcm(100))
    assert buffer.available_bytes == 200


def test_gaps_are_recorded_without_fabricating_silence(buffer: RingBuffer) -> None:
    buffer.append(pcm(500))
    buffer.mark_gap(2000)
    assert buffer.gap_ms == 2000
    # A dropout must not look like a pause in speech.
    assert buffer.position_ms == 500


def test_drift_is_measured_against_wall_clock(buffer: RingBuffer) -> None:
    buffer.append(pcm(1000), arrived_at=START + timedelta(seconds=3))
    assert buffer.drift_seconds(now=START + timedelta(seconds=3)) == pytest.approx(2.0)


def test_empty_window_when_range_is_degenerate(buffer: RingBuffer) -> None:
    buffer.append(pcm(1000))
    assert buffer.window(500, 500).is_empty


def test_samples_decode_to_int16(buffer: RingBuffer) -> None:
    buffer.append(ramp(10))
    values = list(buffer.tail(1.0).samples())
    assert values == list(range(10))
