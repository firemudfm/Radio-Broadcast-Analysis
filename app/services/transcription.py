"""Multilingual ASR behind a service abstraction (ADR-006).

Two passes, for a reason
------------------------
Pass A runs on **every** retained segment, so it is tuned for throughput:
greedy decoding (``beam_size=1``), no word timestamps, automatic language
detection. Its job is recall -- find candidate keyword hits cheaply.

Pass B runs only on audio that pass A found a candidate in, which is a tiny
fraction of the stream, so it can afford to be expensive: wider beam, word
timestamps on, the station's language as a hint, and a prompt built strictly
from approved keyword surface forms. Its job is precision -- confirm or reject.

Spending pass-B settings on the whole stream would not fit the capacity budget
in ADR-008; spending pass-A settings on evidence would produce timestamps too
coarse to cut an audio clip from.

What is preserved
-----------------
The original-language transcript, the detected language and its probability,
segment and word timings, the model name and revision, and the decoding
settings. Nothing is translated by default: a translated transcript cannot
serve as verbatim evidence of what was broadcast.

Models are never downloaded implicitly. The engine loads from a local directory
and fails with a named error if the model is absent, so a container that starts
is a container that can actually transcribe (``docs/MODEL_MANAGEMENT.md``).
"""
from __future__ import annotations

import io
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import Settings
from ..pipeline.enums import AsrPass
from ..pipeline.errors import (
    ModelVerificationError,
    TranscriptionFailedError,
    UnsupportedModelError,
)

logger = logging.getLogger(__name__)

#: Segments whose no-speech probability exceeds this are dropped from the
#: transcript. Whisper hallucinates confidently on non-speech audio, and a
#: hallucinated phrase that happens to contain a keyword is a false mention.
NO_SPEECH_THRESHOLD = 0.85

#: Below this average log-probability a segment is retained but flagged: it is
#: still evidence, just weak evidence.
LOW_CONFIDENCE_LOGPROB = -1.0


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start_ms: int
    end_ms: int
    probability: float | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start_ms: int
    end_ms: int
    words: tuple[TranscriptWord, ...] = ()
    avg_logprob: float | None = None
    no_speech_probability: float | None = None

    @property
    def is_low_confidence(self) -> bool:
        return self.avg_logprob is not None and self.avg_logprob < LOW_CONFIDENCE_LOGPROB


@dataclass(frozen=True)
class TranscriptionResult:
    """One decode of one segment, with everything needed to reproduce it."""

    text: str
    language: str | None
    language_probability: float | None
    segments: tuple[TranscriptSegment, ...]
    model_name: str
    asr_pass: AsrPass
    compute_type: str
    beam_size: int
    duration_ms: int = 0
    model_revision: str | None = None
    dropped_segments: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def words(self) -> tuple[TranscriptWord, ...]:
        return tuple(word for segment in self.segments for word in segment.words)

    def as_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "language_probability": self.language_probability,
            "model": self.model_name,
            "model_revision": self.model_revision,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            "asr_pass": self.asr_pass,
            "duration_ms": self.duration_ms,
            "dropped_segments": self.dropped_segments,
            "segments": [
                {
                    "text": segment.text,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "avg_logprob": segment.avg_logprob,
                    "no_speech_probability": segment.no_speech_probability,
                    "words": [
                        {
                            "text": word.text,
                            "start_ms": word.start_ms,
                            "end_ms": word.end_ms,
                            "probability": word.probability,
                        }
                        for word in segment.words
                    ],
                }
                for segment in self.segments
            ],
        }


@dataclass(frozen=True)
class DecodeOptions:
    """Everything that changes what the decoder produces."""

    beam_size: int = 1
    word_timestamps: bool = False
    language: str | None = None
    initial_prompt: str | None = None
    temperature: float = 0.0
    asr_pass: AsrPass = "a"


@runtime_checkable
class TranscriptionEngine(Protocol):
    """The seam. Swapping ASR implementations must not touch the worker."""

    model_name: str

    def transcribe(self, audio: bytes, options: DecodeOptions) -> TranscriptionResult:
        """Decode one audio segment."""

    def close(self) -> None:
        """Release model resources."""


# --- faster-whisper -----------------------------------------------------------


class FasterWhisperEngine:
    """CTranslate2 / faster-whisper on CPU.

    The model is loaded lazily and guarded by a lock: CTranslate2 model objects
    are not safe to call concurrently from several threads, and a transcription
    worker that shares one across threads corrupts state rather than failing
    cleanly. One model, one caller at a time, is the supported configuration.

    **This engine never downloads anything.** It loads from a local directory
    produced by ``scripts/download-models.py`` and checked by
    ``scripts/verify-models.py``; a missing model is a named, permanent
    :class:`ModelVerificationError` naming the exact commands to run. There is
    deliberately no runtime escape hatch and no HTTP client here -- see
    ``docs/MODEL_MANAGEMENT.md``.
    """

    def __init__(
        self,
        *,
        model_name: str,
        model_root: Path,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 2,
        model_revision: str | None = None,
    ) -> None:
        self.model_name = model_name
        self._model_root = Path(model_root)
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._model_revision = model_revision
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def compute_type(self) -> str:
        return self._compute_type

    def _local_path(self) -> Path:
        """Where ``scripts/download-models.py`` places this model."""
        return self._model_root / "asr" / self.model_name.replace("/", "__")

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415 - heavy optional import
        except ImportError as error:
            raise UnsupportedModelError(
                "faster-whisper is not installed in this image",
                detail="Install the pipeline image, or set RADIO_ASR_BACKEND=fake for tests",
            ) from error

        # A local, already-verified directory is the ONLY accepted source.
        #
        # Passing `self.model_name` to WhisperModel instead would make it fetch
        # from the Hub, which is exactly what this engine must never do: it
        # bypasses models.lock.json, makes a restart depend on a third party
        # being reachable, turns start-up into an unbounded operation, and can
        # place an unverified model on the runtime path. Acquisition is an
        # explicit operator step (scripts/download-models.py), verified by
        # scripts/verify-models.py before workers start.
        local = self._local_path()
        if not local.is_dir():
            raise ModelVerificationError(
                f"ASR model {self.model_name!r} is not present locally",
                detail=(
                    f"Expected {local}.\n"
                    f"Run from the repository root:\n"
                    f"  python3 scripts/download-models.py "
                    f"--root {self._model_root} --role asr\n"
                    f"Then verify:\n"
                    f"  python3 scripts/verify-models.py "
                    f"--root {self._model_root} --role asr"
                ),
            )

        try:
            self._model = WhisperModel(
                str(local),
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
            )
        except Exception as error:  # noqa: BLE001 - classified for the caller
            raise ModelVerificationError(
                f"Could not load ASR model {self.model_name!r}",
                detail=f"{type(error).__name__}: {error}",
            ) from error
        return self._model

    def transcribe(self, audio: bytes, options: DecodeOptions) -> TranscriptionResult:
        model = self._load()
        with self._lock:
            try:
                segments, info = model.transcribe(
                    io.BytesIO(audio),
                    beam_size=options.beam_size,
                    word_timestamps=options.word_timestamps,
                    language=options.language,
                    initial_prompt=options.initial_prompt,
                    temperature=options.temperature,
                    # VAD filtering is left off: classification already happened
                    # upstream, and a second VAD would silently drop the quiet
                    # speech-over-music the policy deliberately retained.
                    vad_filter=False,
                    # Decode each 30-second window independently instead of
                    # conditioning on the previous window's text. Conditioning
                    # is what turns one bad window into a runaway loop -- music
                    # that slipped through classification came out in
                    # production as "right, right, right, ..." repeated for
                    # hundreds of tokens, because each window fed the loop to
                    # the next. The cost is slightly weaker cross-window
                    # consistency; the benefit is that a hallucination cannot
                    # propagate past the window that produced it.
                    condition_on_previous_text=False,
                )
                collected = list(segments)
            except Exception as error:  # noqa: BLE001 - retryable by classification
                raise TranscriptionFailedError(
                    "ASR decoding failed",
                    detail=f"{type(error).__name__}: {error}",
                ) from error

        return _build_result(
            collected,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration_ms=int((getattr(info, "duration", 0.0) or 0.0) * 1000),
            model_name=self.model_name,
            model_revision=self._model_revision,
            compute_type=self._compute_type,
            options=options,
        )

    def close(self) -> None:
        with self._lock:
            self._model = None


def _build_result(
    raw_segments: Sequence[Any],
    *,
    language: str | None,
    language_probability: float | None,
    duration_ms: int,
    model_name: str,
    model_revision: str | None,
    compute_type: str,
    options: DecodeOptions,
) -> TranscriptionResult:
    segments: list[TranscriptSegment] = []
    dropped = 0
    for raw in raw_segments:
        no_speech = getattr(raw, "no_speech_prob", None)
        text = str(getattr(raw, "text", "") or "").strip()
        if not text:
            continue
        if no_speech is not None and no_speech > NO_SPEECH_THRESHOLD:
            # Whisper hallucinates fluent text on non-speech audio. A
            # hallucination containing a tracked brand is a false mention, so
            # these are dropped rather than matched.
            dropped += 1
            continue
        words = tuple(
            TranscriptWord(
                text=str(getattr(word, "word", "") or "").strip(),
                start_ms=int((getattr(word, "start", 0.0) or 0.0) * 1000),
                end_ms=int((getattr(word, "end", 0.0) or 0.0) * 1000),
                probability=getattr(word, "probability", None),
            )
            for word in (getattr(raw, "words", None) or [])
        )
        segments.append(
            TranscriptSegment(
                text=text,
                start_ms=int((getattr(raw, "start", 0.0) or 0.0) * 1000),
                end_ms=int((getattr(raw, "end", 0.0) or 0.0) * 1000),
                words=words,
                avg_logprob=getattr(raw, "avg_logprob", None),
                no_speech_probability=no_speech,
            )
        )

    return TranscriptionResult(
        text=" ".join(segment.text for segment in segments).strip(),
        language=(language or None),
        language_probability=language_probability,
        segments=tuple(segments),
        model_name=model_name,
        model_revision=model_revision,
        compute_type=compute_type,
        beam_size=options.beam_size,
        asr_pass=options.asr_pass,
        duration_ms=duration_ms,
        dropped_segments=dropped,
    )


# --- deterministic fake -------------------------------------------------------


@dataclass
class FakeTranscriptionEngine:
    """Deterministic engine for tests and for running the pipeline without models.

    Not a mock object: it implements the full contract including timings and
    language reporting, so integration tests exercise the real worker, matcher
    and assembler code paths rather than a shortcut around them.
    """

    model_name: str = "fake-whisper"
    compute_type: str = "int8"
    #: Maps a segment's audio digest prefix, or ``"*"``, to the text to return.
    responses: dict[str, str] = field(default_factory=dict)
    default_text: str = ""
    language: str = "en"
    language_probability: float = 0.99
    calls: list[DecodeOptions] = field(default_factory=list)
    failure: Exception | None = None

    def transcribe(self, audio: bytes, options: DecodeOptions) -> TranscriptionResult:
        self.calls.append(options)
        if self.failure is not None:
            raise self.failure

        import hashlib

        digest = hashlib.sha256(audio).hexdigest()
        text = self.responses.get(digest, self.responses.get("*", self.default_text))
        duration_ms = max(1, len(audio) // 32)

        if not text.strip():
            return TranscriptionResult(
                text="",
                language=self.language,
                language_probability=self.language_probability,
                segments=(),
                model_name=self.model_name,
                asr_pass=options.asr_pass,
                compute_type=self.compute_type,
                beam_size=options.beam_size,
                duration_ms=duration_ms,
            )

        words: tuple[TranscriptWord, ...] = ()
        if options.word_timestamps:
            pieces = text.split()
            step = max(1, duration_ms // max(1, len(pieces)))
            words = tuple(
                TranscriptWord(
                    text=piece,
                    start_ms=index * step,
                    end_ms=(index + 1) * step,
                    probability=0.9,
                )
                for index, piece in enumerate(pieces)
            )

        segment = TranscriptSegment(
            text=text,
            start_ms=0,
            end_ms=duration_ms,
            words=words,
            avg_logprob=-0.2,
            no_speech_probability=0.01,
        )
        return TranscriptionResult(
            text=text,
            language=options.language or self.language,
            language_probability=self.language_probability,
            segments=(segment,),
            model_name=self.model_name,
            asr_pass=options.asr_pass,
            compute_type=self.compute_type,
            beam_size=options.beam_size,
            duration_ms=duration_ms,
        )

    def close(self) -> None:
        return None


# --- service ------------------------------------------------------------------


class TranscriptionService:
    """Owns the two-pass strategy and the language-hint policy."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine: TranscriptionEngine | None = None,
        confirmation_engine: TranscriptionEngine | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine or build_engine(settings)
        # Sharing one engine when both passes name the same model avoids
        # holding two copies of the weights in an 8 GiB budget.
        self._confirmation_engine = confirmation_engine or (
            self._engine
            if settings.RADIO_ASR_CONFIRMATION_MODEL == settings.RADIO_ASR_MODEL
            else build_engine(settings, confirmation=True)
        )

    @property
    def engine(self) -> TranscriptionEngine:
        return self._engine

    def transcribe(
        self, audio: bytes, *, language_hints: Sequence[str] = ()
    ) -> TranscriptionResult:
        """Pass A: cheap, high-recall, language-detecting."""
        return self._engine.transcribe(
            audio,
            DecodeOptions(
                beam_size=self._settings.RADIO_ASR_BEAM_SIZE,
                word_timestamps=False,
                language=self._single_language_hint(language_hints),
                asr_pass="a",
            ),
        )

    def confirm(
        self,
        audio: bytes,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        """Pass B: expensive, word-timestamped, keyword-primed."""
        budget = self._settings.RADIO_ASR_PROMPT_MAX_CHARACTERS
        return self._confirmation_engine.transcribe(
            audio,
            DecodeOptions(
                beam_size=self._settings.RADIO_ASR_CONFIRMATION_BEAM_SIZE,
                word_timestamps=True,
                language=language,
                initial_prompt=(prompt or None) if budget > 0 else None,
                asr_pass="b",
            ),
        )

    @staticmethod
    def _single_language_hint(hints: Sequence[str]) -> str | None:
        """Pin the language only when there is exactly one candidate.

        Whisper takes one language, not a list. Forcing the first of several
        hints would break code-switching -- a Hindi-English broadcast pinned to
        ``hi`` loses the English, which is precisely the case this product has
        to handle -- so with two or more hints detection is left to run.
        """
        cleaned = [str(hint).strip().lower() for hint in hints if str(hint).strip()]
        return cleaned[0] if len(cleaned) == 1 else None

    def close(self) -> None:
        self._engine.close()
        if self._confirmation_engine is not self._engine:
            self._confirmation_engine.close()


def build_engine(settings: Settings, *, confirmation: bool = False) -> TranscriptionEngine:
    """Construct the configured ASR backend."""
    if settings.RADIO_ASR_BACKEND == "fake":
        return FakeTranscriptionEngine()
    model = (
        settings.RADIO_ASR_CONFIRMATION_MODEL if confirmation else settings.RADIO_ASR_MODEL
    )
    return FasterWhisperEngine(
        model_name=model,
        model_root=Path(settings.RADIO_MODEL_PATH),
        device=settings.RADIO_ASR_DEVICE,
        compute_type=settings.RADIO_ASR_COMPUTE_TYPE,
        cpu_threads=settings.RADIO_ASR_CPU_THREADS,
    )


__all__ = [
    "LOW_CONFIDENCE_LOGPROB",
    "NO_SPEECH_THRESHOLD",
    "DecodeOptions",
    "FakeTranscriptionEngine",
    "FasterWhisperEngine",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionEngine",
    "TranscriptionResult",
    "TranscriptionService",
    "build_engine",
]
