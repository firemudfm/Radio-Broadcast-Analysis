"""Deterministic synthetic audio for classifier and listener tests.

Real broadcast audio cannot be committed (licensing, size, reproducibility), so
these generators produce signals with the *statistical properties the classifier
actually measures*: pause structure, zero-crossing-rate swing and envelope
variance. They are not a substitute for evaluating against labelled radio --
``docs/QUALITY_EVALUATION.md`` covers that -- but they pin the decision logic so
a refactor cannot silently invert a verdict.

Everything is seeded, so a failure is always reproducible.
"""
from __future__ import annotations

import math
import random
import struct

SAMPLE_RATE = 16_000


def _pack(samples: list[int]) -> bytes:
    clamped = (max(-32768, min(32767, int(value))) for value in samples)
    return struct.pack(f"<{len(samples)}h", *clamped)


def silence(seconds: float, *, sample_rate: int = SAMPLE_RATE, amplitude: int = 2) -> bytes:
    """Near-digital silence with a couple of LSBs of dither."""
    rng = random.Random(11)
    count = int(seconds * sample_rate)
    return _pack([rng.randint(-amplitude, amplitude) for _ in range(count)])


def music_like(
    seconds: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    amplitude: int = 9000,
    seed: int = 7,
) -> bytes:
    """Continuous, harmonically stable tone stack.

    Properties the classifier keys on: no pauses (low ``low_energy_ratio``),
    steady zero-crossing rate, and a flat energy envelope.
    """
    rng = random.Random(seed)
    count = int(seconds * sample_rate)
    partials = [(220.0, 1.0), (330.0, 0.6), (440.0, 0.45), (660.0, 0.3)]
    samples: list[int] = []
    for index in range(count):
        t = index / sample_rate
        # Slow vibrato keeps it from being perfectly periodic without
        # introducing speech-like pauses.
        vibrato = 1.0 + 0.01 * math.sin(2 * math.pi * 5.0 * t)
        value = sum(
            weight * math.sin(2 * math.pi * frequency * vibrato * t)
            for frequency, weight in partials
        )
        samples.append(int(amplitude * value / 2.35 + rng.gauss(0, 40)))
    return _pack(samples)


def speech_like(
    seconds: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    amplitude: int = 9000,
    seed: int = 3,
) -> bytes:
    """Syllable-structured signal alternating voiced, unvoiced and pause.

    Roughly 4 Hz syllabic rate, which is the modulation speech actually has:
    voiced segments are low-zero-crossing tonal bursts, unvoiced segments are
    high-zero-crossing noise, and inter-word pauses drop to near zero.
    """
    rng = random.Random(seed)
    count = int(seconds * sample_rate)
    samples: list[int] = []
    index = 0
    while index < count:
        roll = rng.random()
        if roll < 0.45:
            kind, duration = "voiced", rng.uniform(0.08, 0.18)
        elif roll < 0.75:
            kind, duration = "unvoiced", rng.uniform(0.04, 0.09)
        else:
            kind, duration = "pause", rng.uniform(0.06, 0.20)
        length = min(int(duration * sample_rate), count - index)

        if kind == "voiced":
            f0 = rng.uniform(95.0, 190.0)
            for offset in range(length):
                t = (index + offset) / sample_rate
                # A glottal-ish pulse train: strong low-frequency periodicity.
                value = (
                    math.sin(2 * math.pi * f0 * t)
                    + 0.5 * math.sin(2 * math.pi * 2 * f0 * t)
                    + 0.25 * math.sin(2 * math.pi * 3 * f0 * t)
                )
                envelope = math.sin(math.pi * offset / max(1, length))
                samples.append(int(amplitude * envelope * value / 1.75))
        elif kind == "unvoiced":
            for offset in range(length):
                envelope = math.sin(math.pi * offset / max(1, length))
                samples.append(int(0.35 * amplitude * envelope * rng.uniform(-1.0, 1.0)))
        else:
            samples.extend(rng.randint(-30, 30) for _ in range(length))
        index += length
    return _pack(samples[:count])


def speech_over_music(
    seconds: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    seed: int = 5,
) -> bytes:
    """A voice track mixed over a music bed -- the advertisement case."""
    speech = speech_like(seconds, sample_rate=sample_rate, amplitude=8000, seed=seed)
    bed = music_like(seconds, sample_rate=sample_rate, amplitude=4800, seed=seed + 1)
    return mix(speech, bed)


def mix(*tracks: bytes) -> bytes:
    """Sum PCM tracks, truncating to the shortest."""
    decoded = [
        struct.unpack(f"<{len(track) // 2}h", track[: len(track) - len(track) % 2])
        for track in tracks
    ]
    length = min(len(track) for track in decoded)
    return _pack([sum(track[index] for track in decoded) for index in range(length)])


def concatenate(*tracks: bytes) -> bytes:
    return b"".join(tracks)


__all__ = [
    "SAMPLE_RATE",
    "concatenate",
    "mix",
    "music_like",
    "silence",
    "speech_like",
    "speech_over_music",
]
