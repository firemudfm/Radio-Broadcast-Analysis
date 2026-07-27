"""Transcript-level content classification (ADR-010 §3).

Audio class answers "is this speech"; content type answers "what kind of speech",
which is what campaign policy actually filters on. A spoken advertisement and a
song lyric can both be ``speech_over_music`` in the audio layer and must be
treated completely differently.

Signals used, in combination:

* the transcript itself (lexical cues);
* the audio class the listener assigned;
* duration;
* lexical repetition, which separates song lyrics from prose far more reliably
  than any keyword list.

Honest limits
-------------
The lexical cues are **language-specific and incomplete.** English, Hindi and
Marathi cues are seeded here because they are the priority languages in
``docs/QUALITY_EVALUATION.md``; a Spanish or German broadcast will mostly fall
through to ``unknown``.

That is deliberate and safe: ``unknown`` carries no policy flag, so it is
*included* by every campaign. Guessing ``song_lyrics`` on a language whose cues
have not been evaluated would silently exclude real mentions, which is the
failure this design refuses to risk. Adding a language means adding cues **and**
labelled evaluation data, in that order.

The optional LLM classifier is off by default. It is a refinement over the
rules, never a replacement: a mention exists because the matcher found a
keyword, and no model output can create or destroy one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import Settings
from ..pipeline.enums import CONTENT_TYPE_POLICY_FLAG, ContentType
from .text_normalization import normalized_key, spans_word_boundaries, uses_word_boundaries

logger = logging.getLogger(__name__)

#: Below this many words a transcript carries too little signal to classify.
MIN_WORDS_FOR_CUES = 4

#: A transcript whose distinct-word ratio falls below this, at reasonable
#: length, is repeating itself the way a chorus does rather than the way prose
#: does. Sung content repeats; news copy does not.
LYRIC_REPETITION_RATIO = 0.45

#: Station idents are short by nature.
STATION_ID_MAX_WORDS = 14


@dataclass(frozen=True)
class ContentDecision:
    content_type: ContentType
    confidence: float
    reason: str
    signals: dict[str, float]

    @property
    def policy_flag(self) -> str | None:
        """The campaign flag governing this type, or None when always included."""
        return CONTENT_TYPE_POLICY_FLAG.get(self.content_type)


@runtime_checkable
class ContentClassifier(Protocol):
    name: str

    def classify(
        self,
        transcript: str,
        *,
        audio_class: str = "speech",
        duration_ms: int = 0,
        language: str | None = None,
    ) -> ContentDecision:
        """Label a transcript."""


# --- cue vocabulary -----------------------------------------------------------
#
# Grouped by content type, then by language. Each entry is matched against
# normalised text, so casing and punctuation are already handled.

_CUES: dict[str, dict[str, tuple[str, ...]]] = {
    "emergency_alert": {
        "en": (
            "emergency", "evacuate", "evacuation", "take shelter", "severe weather",
            "tornado warning", "flood warning", "amber alert", "this is not a test",
            "civil defence", "civil defense", "state of emergency",
        ),
        "hi": ("आपातकाल", "चेतावनी", "सतर्क", "तूफान", "बाढ़", "सुरक्षित स्थान"),
        "mr": ("आणीबाणी", "इशारा", "सावधान"),
    },
    "advertisement": {
        "en": (
            "call now", "order now", "buy now", "limited time", "offer ends",
            "discount", "% off", "free shipping", "visit our", "terms and conditions",
            "available at", "in stores now", "book your", "sale ends", "hurry",
        ),
        "hi": ("अभी खरीदें", "छूट", "ऑफर", "मुफ्त", "आज ही", "संपर्क करें", "उपलब्ध"),
        "mr": ("आत्ताच", "सवलत", "ऑफर", "मोफत"),
    },
    "announcement": {
        "en": (
            "please note", "we would like to inform", "will be closed",
            "scheduled for", "reminder that", "starting tomorrow", "road closure",
        ),
        "hi": ("सूचना", "कृपया ध्यान", "घोषणा", "बंद रहेगा"),
        "mr": ("सूचना", "कृपया लक्ष"),
    },
    "news": {
        "en": (
            "according to", "reported", "officials said", "reuters", "associated press",
            "correspondent", "headlines", "police said", "the government",
            "prime minister", "president said", "in a statement",
        ),
        "hi": ("समाचार", "खबर", "सूत्रों के अनुसार", "सरकार ने", "पुलिस ने", "प्रधानमंत्री"),
        "mr": ("बातमी", "सरकारने", "पोलिसांनी"),
    },
    "interview": {
        "en": (
            "thank you for joining", "welcome to the show", "my guest",
            "tell us about", "let me ask you", "joining us now", "in conversation with",
        ),
        "hi": ("हमारे साथ", "बताइए", "आपका स्वागत", "सवाल"),
        "mr": ("आमच्यासोबत", "सांगा"),
    },
    "station_identification": {
        "en": (
            "you're listening to", "you are listening to", "this is",
            "on your radio", "fm", "am", "back after the break", "stay tuned",
        ),
        "hi": ("आप सुन रहे हैं", "रेडियो"),
        "mr": ("तुम्ही ऐकत आहात",),
    },
    "dj_commentary": {
        "en": (
            "that was", "coming up next", "up next", "great track", "here's",
            "let's play", "request", "shout out", "good morning listeners",
        ),
        "hi": ("अगला गाना", "सुनते रहिए"),
        "mr": ("पुढील गाणे",),
    },
}

#: Precomputed normalised cue table, built once at import.
_NORMALISED_CUES: dict[str, tuple[str, ...]] = {
    content_type: tuple(
        sorted({normalized_key(cue) for cues in languages.values() for cue in cues})
    )
    for content_type, languages in _CUES.items()
}

#: Ordered by how costly a miss is. Emergency alerts must never be shadowed by a
#: weaker advertisement or DJ cue.
_PRIORITY: tuple[str, ...] = (
    "emergency_alert",
    "advertisement",
    "announcement",
    "news",
    "interview",
    "station_identification",
    "dj_commentary",
)

_PRICE = re.compile(r"[$€£₹]\s?\d|(?<!\w)\d+\s?(?:%|percent)(?!\w)")
_FREQUENCY = re.compile(r"\b\d{2,3}\.\d\b")


class RulesContentClassifier:
    """Deterministic, inspectable, and explicit about what it cannot see."""

    name = "rules"

    def classify(
        self,
        transcript: str,
        *,
        audio_class: str = "speech",
        duration_ms: int = 0,
        language: str | None = None,
    ) -> ContentDecision:
        text = normalized_key(transcript or "")
        words = text.split() if uses_word_boundaries(transcript or "") else list(text)
        signals: dict[str, float] = {
            "word_count": float(len(words)),
            "duration_ms": float(duration_ms),
        }

        if not text:
            return ContentDecision("unknown", 0.0, "empty transcript", signals)

        repetition = _distinct_ratio(words)
        signals["distinct_word_ratio"] = round(repetition, 4)

        scores = {
            content_type: _cue_hits(text, cues)
            for content_type, cues in _NORMALISED_CUES.items()
        }
        for content_type, hits in scores.items():
            if hits:
                signals[f"cue_{content_type}"] = float(hits)

        # Advertisements are the one type with a strong non-lexical signal.
        if _PRICE.search(transcript or ""):
            scores["advertisement"] = scores.get("advertisement", 0) + 1
            signals["price_pattern"] = 1.0
        if audio_class == "speech_over_music":
            scores["advertisement"] = scores.get("advertisement", 0) + 1
            signals["speech_over_music"] = 1.0

        emergency = scores.get("emergency_alert", 0)
        if emergency:
            return ContentDecision(
                "emergency_alert",
                min(0.95, 0.6 + 0.15 * emergency),
                "emergency vocabulary present",
                signals,
            )

        if audio_class in {"singing", "music"}:
            # The listener's rolling classifier already concluded this is sung
            # or musical content; that IS the primary signal. The only question
            # left is whether it is a sung *advertisement*, which is what the
            # jingle policy exists to retain.
            selling = scores.get("advertisement", 0) or scores.get("announcement", 0)
            if selling:
                return ContentDecision(
                    "advertisement",
                    min(0.85, 0.55 + 0.1 * selling),
                    "sung content carrying advertising vocabulary",
                    signals,
                )
            confident = len(words) >= 12 and repetition < LYRIC_REPETITION_RATIO
            signals["lyric_repetition"] = 1.0 if confident else 0.0
            return ContentDecision(
                "song_lyrics",
                0.75 if confident else 0.55,
                (
                    "sung audio with prose-atypical repetition"
                    if confident
                    else "sung audio with no advertising vocabulary"
                ),
                signals,
            )

        if len(words) < MIN_WORDS_FOR_CUES:
            if _FREQUENCY.search(transcript or "") or scores.get("station_identification"):
                return ContentDecision(
                    "station_identification", 0.6, "short ident-shaped utterance", signals
                )
            # Too little text to classify. `unknown` carries no policy flag, so
            # it stays included rather than being filtered out on a guess.
            return ContentDecision("unknown", 0.2, "transcript too short to classify", signals)

        best = max(_PRIORITY, key=lambda content_type: (scores.get(content_type, 0),))
        hits = scores.get(best, 0)
        if hits == 0:
            return self._fallback(audio_class, words, signals)

        if best == "station_identification" and len(words) > STATION_ID_MAX_WORDS:
            # "This is..." appears in plenty of long-form speech; only a short
            # utterance is actually an ident.
            best = "dj_commentary" if audio_class == "speech_over_music" else "discussion"
            return ContentDecision(best, 0.4, "ident cue in long-form speech", signals)

        return ContentDecision(
            best,  # type: ignore[arg-type]
            min(0.9, 0.5 + 0.12 * hits),
            f"{hits} lexical cue(s) for {best}",
            signals,
        )

    @staticmethod
    def _fallback(
        audio_class: str, words: list[str], signals: dict[str, float]
    ) -> ContentDecision:
        if audio_class == "speech_over_music":
            return ContentDecision(
                "dj_commentary", 0.35, "speech over music with no stronger cue", signals
            )
        if len(words) >= 60:
            return ContentDecision(
                "discussion", 0.35, "long-form speech with no stronger cue", signals
            )
        return ContentDecision("unknown", 0.25, "no lexical cue matched", signals)


class PassthroughContentClassifier:
    """Labels everything ``unknown``, i.e. included by every campaign policy."""

    name = "passthrough"

    def classify(
        self,
        transcript: str,
        *,
        audio_class: str = "speech",
        duration_ms: int = 0,
        language: str | None = None,
    ) -> ContentDecision:
        return ContentDecision("unknown", 0.0, "content classification disabled", {})


# --- policy -------------------------------------------------------------------


def is_included(content_type: str, policy: dict[str, bool]) -> tuple[bool, str | None]:
    """Whether a campaign's content policy admits this content type.

    Returns ``(included, exclusion_reason)``. A type with no policy flag is
    always included, which is what keeps ``unknown`` safe as a default.
    """
    flag = CONTENT_TYPE_POLICY_FLAG.get(content_type)
    if flag is None:
        return True, None
    if policy.get(flag, True):
        return True, None
    return False, f"{flag} is disabled for this campaign"


def build_content_classifier(settings: Settings) -> ContentClassifier:
    if settings.RADIO_CONTENT_CLASSIFIER == "passthrough":
        return PassthroughContentClassifier()
    return RulesContentClassifier()


# --- helpers ------------------------------------------------------------------


def _cue_hits(text: str, cues: tuple[str, ...]) -> int:
    return sum(1 for cue in cues if cue and _contains_cue(text, cue))


def _contains_cue(text: str, cue: str) -> bool:
    """Word-boundary-aware containment.

    Plain substring matching is wrong here and dangerously so: the short cues
    ("am", "fm", "this is") occur inside ordinary words, and "am" inside
    "Amazon" was enough to label a song as a station ident -- which flipped it
    from a policy-excluded type to an included one. Boundaries are skipped for
    scripts that have none, exactly as in the keyword matcher.
    """
    if not uses_word_boundaries(cue):
        return cue in text
    start = text.find(cue)
    while start != -1:
        if spans_word_boundaries(text, start, start + len(cue)):
            return True
        start = text.find(cue, start + 1)
    return False


def _distinct_ratio(words: list[str]) -> float:
    if not words:
        return 1.0
    return len(set(words)) / len(words)


__all__ = [
    "LYRIC_REPETITION_RATIO",
    "MIN_WORDS_FOR_CUES",
    "ContentClassifier",
    "ContentDecision",
    "PassthroughContentClassifier",
    "RulesContentClassifier",
    "build_content_classifier",
    "is_included",
]
