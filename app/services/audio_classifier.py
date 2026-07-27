"""Pluggable audio classification (ADR-005).

What this module is honest about
--------------------------------
Separating *speech* from *singing* is not solved by a voice-activity detector.
Silero VAD answers "is there a human voice here", and a sung vocal is a human
voice, so VAD alone labels most songs as speech. YAMNet-class audio-event models
do better, but their TensorFlow dependency is not currently practical on this
target (see ``docs/research/TECHNOLOGY_RESEARCH.md``), so the ``yamnet`` backend
is declared and refuses to start rather than silently substituting something
else.

The default ``vad_energy`` backend therefore combines several classic
speech/music discrimination features rather than trusting any single one:

``low_energy_ratio``
    Fraction of frames quieter than half the window mean. Speech is full of
    short pauses between phones and words; sustained music is not. This is the
    single most informative cheap feature in the literature.
``zcr_variance``
    Speech alternates voiced (low zero-crossing-rate) and unvoiced (high)
    segments, so its ZCR swings. Steady instrumentation swings much less.
``energy_variance``
    Variance of the log-energy contour -- the syllabic envelope.
``silence_ratio``
    Fraction of frames below an absolute floor, which separates true silence
    from quiet content.
``speech_probability``
    Silero VAD, when the model is actually present and loadable. Treated as one
    vote, never as the answer.

Every threshold below is a **provisional starting point**, not a measured
optimum. They must be evaluated against labelled audio before any of them is
called production-ready; ``docs/QUALITY_EVALUATION.md`` defines that process.

Why the policy is recall-first
------------------------------
A false "music" verdict deletes audio that no later stage can recover -- the
mention is lost silently and forever. A false "speech" verdict costs one cheap
transcription that the keyword matcher then discards. The costs are not
symmetric, so:

* uncertain audio is transcribed (``RADIO_TRANSCRIBE_UNCERTAIN_AUDIO``);
* nothing is ever discarded on a single frame or a single window;
* discard requires the same confident verdict sustained for
  ``RADIO_PURE_MUSIC_DISCARD_SECONDS``.

Precision is recovered later, from the transcript, where the evidence is
inspectable (ADR-010).
"""
from __future__ import annotations

import logging
import math
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Settings
from ..pipeline.enums import AudioContentClass
from ..pipeline.errors import ClassifierUnavailableError
from .ring_buffer import PcmWindow

logger = logging.getLogger(__name__)

#: Full-scale amplitude for signed 16-bit PCM.
_FULL_SCALE = 32768.0

#: Absolute silence floor in dBFS. Below this a frame carries no usable content
#: at any realistic broadcast level.
SILENCE_DBFS = -50.0

#: Provisional decision thresholds. See the module docstring: these are
#: starting points for evaluation, not measured optima.
SPEECH_LOW_ENERGY_RATIO = 0.22
MUSIC_LOW_ENERGY_RATIO = 0.14
SPEECH_ZCR_VARIANCE = 0.0035
SPEECH_ENERGY_VARIANCE = 12.0
VAD_SPEECH_VOTE = 0.5

#: Reduced speech bar applied only when music evidence is already strong.
#:
#: A music bed physically fills the inter-word pauses that ``low_energy_ratio``
#: and ``energy_variance`` measure, so speech evidence is *systematically*
#: suppressed by the presence of music -- not because there is less speech.
#: Requiring the full 0.5 threshold here labelled spoken advertisements over a
#: loud bed as ``singing``, which is a discard candidate: exactly the mention
#: this product exists to catch. Measured separation is wide (pure instrumental
#: music scores ~0.00 on this axis), so the lower bar costs little precision.
SPEECH_OVER_MUSIC_FLOOR = 0.25


@dataclass(frozen=True)
class FrameFeatures:
    """Per-frame measurements. Cheap enough to run on every active station."""

    rms: float
    dbfs: float
    zero_crossing_rate: float

    @property
    def is_silent(self) -> bool:
        return self.dbfs <= SILENCE_DBFS


@dataclass(frozen=True)
class ClassificationResult:
    """One window's verdict, with the evidence that produced it."""

    content_class: AudioContentClass
    confidence: float
    signals: dict[str, float]
    reason: str
    start_ms: int = 0
    duration_ms: int = 0

    @property
    def is_discardable(self) -> bool:
        return self.content_class in {"silence", "music", "singing"}


@runtime_checkable
class AudioClassifier(Protocol):
    """The seam every backend implements.

    Kept deliberately small so that replacing the heuristic with a real audio
    event model is a drop-in change rather than a rewrite of the listener.
    """

    name: str

    def classify(self, window: PcmWindow) -> ClassificationResult:
        """Label one window of PCM."""

    def reset(self) -> None:
        """Drop any per-stream state, e.g. after a reconnect."""


# --- feature extraction -------------------------------------------------------


def frame_features(samples: array, start: int, end: int) -> FrameFeatures:
    """RMS, dBFS and zero-crossing rate for ``samples[start:end]``.

    Pure Python on purpose: the base install has no numpy, and this must work
    on the deployment target as shipped. At 16 kHz and 32 ms frames this is
    ~512 arithmetic operations per frame, which is affordable for the
    conservative station counts in ADR-008.
    """
    count = end - start
    if count <= 0:
        return FrameFeatures(rms=0.0, dbfs=-120.0, zero_crossing_rate=0.0)

    total = 0.0
    crossings = 0
    previous = samples[start]
    for index in range(start, end):
        value = samples[index]
        total += float(value) * float(value)
        if (value >= 0) != (previous >= 0):
            crossings += 1
        previous = value

    rms = math.sqrt(total / count) / _FULL_SCALE
    dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
    return FrameFeatures(
        rms=rms,
        dbfs=dbfs,
        zero_crossing_rate=crossings / max(1, count - 1),
    )


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


@dataclass(frozen=True)
class WindowSignals:
    """Aggregate features over a multi-second window."""

    frame_count: int
    mean_dbfs: float
    peak_dbfs: float
    silence_ratio: float
    low_energy_ratio: float
    zcr_mean: float
    zcr_variance: float
    energy_variance: float
    speech_probability: float | None = None

    def as_dict(self) -> dict[str, float]:
        signals = {
            "frame_count": float(self.frame_count),
            "mean_dbfs": round(self.mean_dbfs, 2),
            "peak_dbfs": round(self.peak_dbfs, 2),
            "silence_ratio": round(self.silence_ratio, 4),
            "low_energy_ratio": round(self.low_energy_ratio, 4),
            "zcr_mean": round(self.zcr_mean, 5),
            "zcr_variance": round(self.zcr_variance, 6),
            "energy_variance": round(self.energy_variance, 3),
        }
        if self.speech_probability is not None:
            signals["speech_probability"] = round(self.speech_probability, 4)
        return signals


def analyse_window(
    window: PcmWindow,
    *,
    frame_ms: int = 32,
    speech_probability: float | None = None,
) -> WindowSignals:
    """Reduce a PCM window to the aggregate signals the policy reads."""
    samples = window.samples()
    frame_size = max(1, (window.sample_rate * frame_ms) // 1000)
    frame_count = len(samples) // frame_size
    if frame_count == 0:
        return WindowSignals(
            frame_count=0,
            mean_dbfs=-120.0,
            peak_dbfs=-120.0,
            silence_ratio=1.0,
            low_energy_ratio=0.0,
            zcr_mean=0.0,
            zcr_variance=0.0,
            energy_variance=0.0,
            speech_probability=speech_probability,
        )

    features = [
        frame_features(samples, index * frame_size, (index + 1) * frame_size)
        for index in range(frame_count)
    ]
    rms_values = [item.rms for item in features]
    dbfs_values = [item.dbfs for item in features]
    zcr_values = [item.zero_crossing_rate for item in features]

    mean_rms = sum(rms_values) / len(rms_values)
    silent = sum(1 for item in features if item.is_silent)
    # "Low energy" is relative to this window's own mean, so the measure is
    # independent of station loudness and of any upstream normalisation.
    low_energy = sum(1 for value in rms_values if value < 0.5 * mean_rms)

    return WindowSignals(
        frame_count=frame_count,
        mean_dbfs=sum(dbfs_values) / len(dbfs_values),
        peak_dbfs=max(dbfs_values),
        silence_ratio=silent / frame_count,
        low_energy_ratio=low_energy / frame_count,
        zcr_mean=sum(zcr_values) / len(zcr_values),
        zcr_variance=_variance(zcr_values),
        energy_variance=_variance(dbfs_values),
        speech_probability=speech_probability,
    )


# --- Silero VAD (optional) ----------------------------------------------------


class SileroVad:
    """Optional ONNX Runtime wrapper around Silero VAD.

    Optional in the strong sense: neither ``onnxruntime`` nor the model file is
    a hard dependency, and every failure path degrades to "no VAD signal"
    instead of taking the listener down. A voice-activity detector that crashes
    a live capture is worse than no voice-activity detector.

    The calling convention differs between Silero v4 (``h``/``c`` state tensors)
    and v5 (a single fused ``state`` tensor, 512-sample chunks at 16 kHz). Both
    are attempted, the working one is remembered, and repeated failures disable
    the detector for the process rather than logging on every frame.
    """

    #: Silero v5 requires exactly this many samples per call at 16 kHz.
    CHUNK_SAMPLES_16K = 512
    CHUNK_SAMPLES_8K = 256

    #: Give up after this many consecutive inference failures.
    MAX_FAILURES = 3

    def __init__(self, model_path: Path, *, sample_rate: int = 16_000) -> None:
        self._model_path = Path(model_path)
        self._sample_rate = sample_rate
        self._session = None
        self._numpy = None
        self._convention: str | None = None
        self._state = None
        self._failures = 0
        self._available = self._load()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def chunk_samples(self) -> int:
        return self.CHUNK_SAMPLES_8K if self._sample_rate == 8_000 else self.CHUNK_SAMPLES_16K

    def _load(self) -> bool:
        if not self._model_path.is_file():
            logger.info(
                "Silero VAD model not present; continuing with energy signals only",
                extra={"model_present": False},
            )
            return False
        try:
            import numpy  # noqa: PLC0415 - optional dependency, imported on demand
            import onnxruntime  # noqa: PLC0415
        except ImportError:
            logger.info("onnxruntime is not installed; continuing with energy signals only")
            return False
        try:
            options = onnxruntime.SessionOptions()
            # One thread: this runs inside a listener that is already supervising
            # several stations, and an ORT thread pool per station would
            # oversubscribe the 4 vCPUs the design targets.
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            self._session = onnxruntime.InferenceSession(
                str(self._model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self._numpy = numpy
        except Exception as error:  # noqa: BLE001 - any load failure is non-fatal
            logger.warning("Silero VAD failed to load: %s", type(error).__name__)
            return False
        self.reset()
        return True

    def reset(self) -> None:
        """Clear recurrent state. Required at every stream discontinuity."""
        if self._numpy is None:
            return
        self._state = None
        self._convention = None

    def speech_probability(self, window: PcmWindow) -> float | None:
        """Mean speech probability over ``window``, or None when unavailable."""
        if not self._available or self._numpy is None or self._session is None:
            return None
        numpy = self._numpy
        samples = window.samples()
        chunk = self.chunk_samples
        chunks = len(samples) // chunk
        if chunks == 0:
            return None

        scores: list[float] = []
        for index in range(chunks):
            block = samples[index * chunk : (index + 1) * chunk]
            audio = numpy.asarray(block, dtype=numpy.float32).reshape(1, -1) / _FULL_SCALE
            score = self._infer(audio)
            if score is None:
                return None
            scores.append(score)
        return sum(scores) / len(scores) if scores else None

    def _infer(self, audio) -> float | None:
        numpy = self._numpy
        assert numpy is not None and self._session is not None  # nosec B101 - guarded by caller
        conventions = (self._convention,) if self._convention else ("v5", "v4")
        for convention in conventions:
            try:
                if convention == "v5":
                    if self._state is None:
                        self._state = numpy.zeros((2, 1, 128), dtype=numpy.float32)
                    outputs = self._session.run(
                        None,
                        {
                            "input": audio,
                            "state": self._state,
                            "sr": numpy.array(self._sample_rate, dtype=numpy.int64),
                        },
                    )
                    self._state = outputs[1]
                else:
                    if self._state is None or not isinstance(self._state, tuple):
                        zeros = numpy.zeros((2, 1, 64), dtype=numpy.float32)
                        self._state = (zeros, zeros.copy())
                    hidden, cell = self._state
                    outputs = self._session.run(
                        None,
                        {
                            "input": audio,
                            "sr": numpy.array(self._sample_rate, dtype=numpy.int64),
                            "h": hidden,
                            "c": cell,
                        },
                    )
                    self._state = (outputs[1], outputs[2])
                self._convention = convention
                self._failures = 0
                return float(numpy.asarray(outputs[0]).reshape(-1)[0])
            except Exception:  # noqa: BLE001, S112 - try the other convention
                self._state = None
                continue

        self._failures += 1
        if self._failures >= self.MAX_FAILURES:
            logger.warning(
                "Silero VAD disabled after %d consecutive inference failures", self._failures
            )
            self._available = False
        return None


# --- backends -----------------------------------------------------------------


class PassthroughClassifier:
    """Labels everything ``speech``.

    Not a test double -- it is a legitimate production setting for an operator
    who wants maximum recall and is willing to pay for transcribing music. It
    is also the safe fallback when a real classifier cannot be constructed.
    """

    name = "passthrough"

    def classify(self, window: PcmWindow) -> ClassificationResult:
        return ClassificationResult(
            content_class="speech",
            confidence=0.5,
            signals={},
            reason="passthrough backend transcribes everything",
            start_ms=window.start_offset_ms,
            duration_ms=window.duration_ms,
        )

    def reset(self) -> None:
        return None


class VadEnergyClassifier:
    """Default backend: energy/ZCR statistics, plus Silero VAD when present."""

    name = "vad_energy"

    def __init__(
        self,
        *,
        frame_ms: int = 32,
        speech_threshold: float = 0.5,
        vad: SileroVad | None = None,
    ) -> None:
        self._frame_ms = frame_ms
        self._speech_threshold = speech_threshold
        self._vad = vad

    @property
    def vad_available(self) -> bool:
        return bool(self._vad and self._vad.available)

    def reset(self) -> None:
        if self._vad is not None:
            self._vad.reset()

    def classify(self, window: PcmWindow) -> ClassificationResult:
        probability = self._vad.speech_probability(window) if self._vad else None
        signals = analyse_window(
            window, frame_ms=self._frame_ms, speech_probability=probability
        )
        content_class, confidence, reason = self._decide(signals)
        return ClassificationResult(
            content_class=content_class,
            confidence=confidence,
            signals=signals.as_dict(),
            reason=reason,
            start_ms=window.start_offset_ms,
            duration_ms=window.duration_ms,
        )

    def _decide(self, signals: WindowSignals) -> tuple[AudioContentClass, float, str]:
        if signals.frame_count == 0:
            return "unknown", 0.0, "window too short to measure"

        if signals.silence_ratio >= 0.95 or signals.peak_dbfs <= SILENCE_DBFS:
            return "silence", min(0.99, 0.6 + signals.silence_ratio * 0.4), "below the silence floor"

        # Two independent scores rather than one axis: content can be both
        # (speech over music), and collapsing them would make that class
        # unreachable.
        speech_score = self._speech_score(signals)
        music_score = self._music_score(signals)

        if signals.speech_probability is not None:
            # VAD is one vote. It answers "human voice present", which a sung
            # vocal also satisfies, so it raises the speech score but is never
            # allowed to veto the music evidence on its own.
            vote = 1.0 if signals.speech_probability >= self._speech_threshold else 0.0
            speech_score = 0.6 * speech_score + 0.4 * vote

        speechy = speech_score >= 0.5
        musical = music_score >= 0.5
        if musical and not speechy:
            # See SPEECH_OVER_MUSIC_FLOOR: music suppresses the pause structure
            # the speech features rely on, so the bar drops rather than letting
            # a spoken advertisement fall through to a discardable class.
            vad_says_voice = (
                signals.speech_probability is not None
                and signals.speech_probability >= self._speech_threshold
            )
            if speech_score >= SPEECH_OVER_MUSIC_FLOOR or vad_says_voice:
                speechy = True
        margin = abs(speech_score - music_score)

        if speechy and musical:
            return (
                "speech_over_music",
                min(0.9, 0.45 + margin),
                "speech-like modulation over sustained musical energy",
            )
        if speechy and not musical:
            return "speech", min(0.95, 0.5 + speech_score / 2), "speech-like energy modulation"
        if musical and not speechy:
            # Sung vocals and instrumental music are not separable with these
            # features alone. Anything with voice-like evidence is reported as
            # `singing`, which the policy layer treats more cautiously than
            # pure `music` (ADR-010).
            voiced = (
                signals.speech_probability is not None
                and signals.speech_probability >= self._speech_threshold
            ) or signals.zcr_variance >= SPEECH_ZCR_VARIANCE * 0.5
            if voiced:
                return "singing", min(0.8, 0.4 + music_score / 2), "sustained music with voice-like content"
            return "music", min(0.9, 0.45 + music_score / 2), "sustained non-modulated energy"

        # Neither hypothesis is supported. Recall-first: this is transcribed.
        return "unknown", max(0.1, 0.5 - margin), "signals do not favour speech or music"

    @staticmethod
    def _speech_score(signals: WindowSignals) -> float:
        """Evidence for speech: pauses, ZCR swing, envelope variation."""
        pauses = _ramp(signals.low_energy_ratio, MUSIC_LOW_ENERGY_RATIO, SPEECH_LOW_ENERGY_RATIO)
        zcr = _ramp(signals.zcr_variance, SPEECH_ZCR_VARIANCE * 0.4, SPEECH_ZCR_VARIANCE)
        envelope = _ramp(signals.energy_variance, SPEECH_ENERGY_VARIANCE * 0.4, SPEECH_ENERGY_VARIANCE)
        return 0.45 * pauses + 0.3 * zcr + 0.25 * envelope

    @staticmethod
    def _music_score(signals: WindowSignals) -> float:
        """Evidence for music: continuous energy, steady ZCR, flat envelope."""
        continuity = 1.0 - _ramp(
            signals.low_energy_ratio, MUSIC_LOW_ENERGY_RATIO, SPEECH_LOW_ENERGY_RATIO
        )
        steadiness = 1.0 - _ramp(signals.zcr_variance, SPEECH_ZCR_VARIANCE * 0.4, SPEECH_ZCR_VARIANCE)
        flatness = 1.0 - _ramp(
            signals.energy_variance, SPEECH_ENERGY_VARIANCE * 0.4, SPEECH_ENERGY_VARIANCE
        )
        loud = _ramp(signals.mean_dbfs, -45.0, -30.0)
        return 0.4 * continuity + 0.25 * steadiness + 0.25 * flatness + 0.10 * loud


class YamnetClassifier:
    """Reserved backend. Refuses to start rather than degrading silently.

    Selecting ``RADIO_AUDIO_CLASSIFIER=yamnet`` is a deliberate operator
    request for a specific model. Quietly serving a different one would make
    every downstream quality measurement a lie about what produced it, so this
    raises. The blocker is recorded in ``docs/research/TECHNOLOGY_RESEARCH.md``
    and ADR-005.
    """

    name = "yamnet"

    def __init__(self) -> None:
        raise ClassifierUnavailableError(
            "The yamnet audio classifier is not deployable on this target",
            detail=(
                "YAMNet requires TensorFlow, which has no supported linux/arm64 "
                "CPU wheel for Python 3.11 within the 8 GiB budget. See "
                "docs/research/TECHNOLOGY_RESEARCH.md. Use RADIO_AUDIO_CLASSIFIER=vad_energy."
            ),
        )

    def classify(self, window: PcmWindow) -> ClassificationResult:  # pragma: no cover - unreachable
        raise ClassifierUnavailableError("yamnet is unavailable")

    def reset(self) -> None:  # pragma: no cover - unreachable
        return None


# --- rolling policy -----------------------------------------------------------


@dataclass
class _Run:
    content_class: AudioContentClass = "unknown"
    duration_ms: int = 0
    confidence_sum: float = 0.0
    windows: int = 0

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.windows if self.windows else 0.0


@dataclass(frozen=True)
class Decision:
    """What the listener should actually do with a window."""

    content_class: AudioContentClass
    keep: bool
    confidence: float
    reason: str
    run_ms: int
    signals: dict[str, float] = field(default_factory=dict)


class RollingAudioPolicy:
    """Turns per-window verdicts into keep/discard decisions with hysteresis.

    The rule that matters: **audio is never discarded on one window.** A
    discardable class must hold, confidently, for
    ``RADIO_PURE_MUSIC_DISCARD_SECONDS`` (or ``RADIO_SILENCE_END_SECONDS`` for
    silence) before anything is dropped. Until then the audio is kept, because
    a wrongly dropped mention cannot be recovered and a wrongly kept one costs
    one cheap transcription.

    It also recognises jingles: a short musical run bracketed by recent speech
    is advertising context, not a song, and is retained (ADR-010).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._run = _Run()
        self._last_speech_end_ms: int | None = None
        self._position_ms = 0

    def reset(self) -> None:
        self._run = _Run()
        self._last_speech_end_ms = None
        self._position_ms = 0

    @property
    def current_run_ms(self) -> int:
        return self._run.duration_ms

    @property
    def current_class(self) -> AudioContentClass:
        return self._run.content_class

    def observe(self, result: ClassificationResult) -> Decision:
        """Fold one classification into the rolling state and decide."""
        duration = max(0, result.duration_ms)
        self._position_ms += duration

        if result.content_class == self._run.content_class:
            self._run.duration_ms += duration
            self._run.confidence_sum += result.confidence
            self._run.windows += 1
        else:
            self._run = _Run(
                content_class=result.content_class,
                duration_ms=duration,
                confidence_sum=result.confidence,
                windows=1,
            )

        if result.content_class in {"speech", "speech_over_music"}:
            self._last_speech_end_ms = self._position_ms

        keep, reason = self._should_keep(result)
        return Decision(
            content_class=result.content_class,
            keep=keep,
            confidence=result.confidence,
            reason=reason,
            run_ms=self._run.duration_ms,
            signals=result.signals,
        )

    def _should_keep(self, result: ClassificationResult) -> tuple[bool, str]:
        settings = self._settings
        content_class = result.content_class

        if content_class in {"speech", "jingle"}:
            return True, "speech is always transcribed"
        if content_class == "speech_over_music":
            if settings.RADIO_INCLUDE_SPEECH_OVER_MUSIC:
                return True, "speech over music is retained by policy"
            return False, "RADIO_INCLUDE_SPEECH_OVER_MUSIC is disabled"
        if content_class == "unknown":
            if settings.RADIO_TRANSCRIBE_UNCERTAIN_AUDIO:
                return True, "uncertain audio is transcribed to protect recall"
            return False, "RADIO_TRANSCRIBE_UNCERTAIN_AUDIO is disabled"

        if content_class == "silence":
            threshold_ms = settings.RADIO_SILENCE_END_SECONDS * 1000
            if self._run.duration_ms < threshold_ms:
                return True, "short silence may be a pause inside speech"
            return False, f"silence sustained beyond {settings.RADIO_SILENCE_END_SECONDS}s"

        # music / singing
        threshold_ms = settings.RADIO_PURE_MUSIC_DISCARD_SECONDS * 1000
        if self._run.duration_ms < threshold_ms:
            return True, "music run is too short to be confidently a song"
        if self._run.mean_confidence < 0.55:
            return True, "music verdict is not confident enough to discard"
        if self._is_jingle_context():
            return True, (
                f"provisional jingle allowance: musical run adjacent to speech, "
                f"retained up to {settings.RADIO_JINGLE_MAX_SECONDS}s"
            )
        if content_class == "singing" and settings.RADIO_INCLUDE_LONG_FORM_SINGING:
            return True, "RADIO_INCLUDE_LONG_FORM_SINGING is enabled"
        return False, f"{content_class} sustained for {self._run.duration_ms}ms with confidence"

    def _is_jingle_context(self) -> bool:
        """Whether a musical run may still turn out to be an advertising jingle.

        Whether music is a jingle or a song is only knowable once it *ends*, so
        retention has to be provisional: music that follows speech is kept until
        the run exceeds ``RADIO_JINGLE_MAX_SECONDS``, then discarded.

        The bounded cost is real and worth stating plainly: on a station where
        the DJ speaks before every track, the first ``RADIO_JINGLE_MAX_SECONDS``
        of each song is retained and transcribed (~90 KB of Opus at the default
        bitrate). That is the price of not silently deleting sung advertising,
        and it is why the setting is tunable per deployment. Lower it if songs
        dominate the spool; raise it if long jingles are being missed.
        """
        settings = self._settings
        if not settings.RADIO_INCLUDE_SUNG_ADVERTISING_JINGLES:
            return False
        if self._run.duration_ms > settings.RADIO_JINGLE_MAX_SECONDS * 1000:
            return False
        if self._last_speech_end_ms is None:
            return False
        gap_ms = self._position_ms - self._run.duration_ms - self._last_speech_end_ms
        return 0 <= gap_ms <= settings.RADIO_JINGLE_ADJACENCY_SECONDS * 1000


# --- construction -------------------------------------------------------------


def _ramp(value: float, low: float, high: float) -> float:
    """Linear 0..1 ramp between ``low`` and ``high``, clamped at both ends."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def build_classifier(settings: Settings) -> AudioClassifier:
    """Construct the configured backend.

    An unknown value cannot reach here -- ``RADIO_AUDIO_CLASSIFIER`` is a
    ``Literal`` validated at settings load, so a typo fails at startup rather
    than selecting a silent no-op.
    """
    backend = settings.RADIO_AUDIO_CLASSIFIER
    if backend == "passthrough":
        return PassthroughClassifier()
    if backend == "yamnet":
        return YamnetClassifier()

    vad_path = Path(settings.RADIO_MODEL_PATH) / "vad" / settings.RADIO_VAD_MODEL_FILENAME
    vad = SileroVad(vad_path, sample_rate=settings.RADIO_SAMPLE_RATE)
    if not vad.available:
        logger.info(
            "Audio classification running on energy signals only",
            extra={"classifier": "vad_energy", "vad_available": False},
        )
    return VadEnergyClassifier(
        frame_ms=settings.RADIO_CLASSIFIER_FRAME_MS,
        speech_threshold=settings.RADIO_VAD_SPEECH_THRESHOLD,
        vad=vad if vad.available else None,
    )


__all__ = [
    "SILENCE_DBFS",
    "AudioClassifier",
    "ClassificationResult",
    "Decision",
    "FrameFeatures",
    "PassthroughClassifier",
    "RollingAudioPolicy",
    "SileroVad",
    "VadEnergyClassifier",
    "WindowSignals",
    "YamnetClassifier",
    "analyse_window",
    "build_classifier",
    "frame_features",
]
