"""Unicode-aware normalisation for multilingual keyword matching.

The existing `app/text.py::normalize_text` is NFKD + casefold + token join. That
is right for ASCII-ish Latin text and wrong for several of the languages this
product targets, because it silently destroys meaning:

* **Devanagari** — NFKD decomposes and stripping combining marks would remove
  matras (vowel signs). ``किताब`` and ``कताब`` are different words.
* **Arabic/Hebrew** — diacritics are often optional in writing, so stripping
  them *helps* recall.
* **CJK/Thai/Lao/Khmer/Japanese** — no word delimiters, so whitespace tokens do
  not exist and word-boundary matching cannot use ``\\b``.

So this module offers two normalisation levels and picks boundary rules from the
script actually present, rather than applying one Latin-shaped rule everywhere.
`app/text.py` is left untouched: it is load-bearing for existing entity ids.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

#: Scripts whose combining marks carry lexical meaning and must be preserved.
#: Removing a matra changes the word, so diacritic folding is refused here.
MARK_SIGNIFICANT_SCRIPTS: frozenset[str] = frozenset(
    {
        "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA", "TAMIL",
        "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "THAI", "LAO", "TIBETAN",
        "MYANMAR", "KHMER",
    }
)

#: Scripts written without spaces between words. Token-boundary matching is
#: meaningless for these; substring matching on normalised text is used instead.
NO_WORD_BOUNDARY_SCRIPTS: frozenset[str] = frozenset(
    {"HAN", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR", "TIBETAN"}
)

#: Punctuation and symbol categories collapsed to a single space.
_SEPARATOR_CATEGORIES = frozenset({"Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Sm", "Sk", "So", "Sc"})

_WHITESPACE = re.compile(r"\s+")

#: Codepoints that are invisible or direction-controlling. Left in place they
#: would let a keyword be defeated by an unprintable character, and they can
#: make two visually identical strings compare unequal.
#:
#: Declared as NUMERIC RANGES, never as literal characters: a source file that
#: contains real bidirectional controls is a Trojan Source hazard (the rendered
#: code can differ from what the interpreter executes), which `bandit` flags as
#: high severity. Numbers cannot lie about what they are.
#:
#: Note U+200C/U+200D (ZWNJ/ZWJ): these are orthographically meaningful in
#: several Indic and Perso-Arabic scripts. They are stripped from the MATCHING
#: KEY only -- it raises recall (a keyword written with or without a joiner
#: still matches) and the original text is preserved for evidence through the
#: offset map, so nothing shown to a user is altered.
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x180E, 0x180E),  # MONGOLIAN VOWEL SEPARATOR
    (0x200B, 0x200F),  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    (0x202A, 0x202E),  # bidi embedding and override
    (0x2060, 0x2064),  # word joiner, invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE / BOM
    (0xFFF9, 0xFFFB),  # interlinear annotation
)

_INVISIBLE_CODEPOINTS: frozenset[int] = frozenset(
    codepoint
    for low, high in _INVISIBLE_RANGES
    for codepoint in range(low, high + 1)
)


def is_invisible(character: str) -> bool:
    """Whether ``character`` is an invisible or direction-controlling codepoint."""
    return ord(character) in _INVISIBLE_CODEPOINTS


@dataclass(frozen=True)
class NormalizedText:
    """Normalised text plus a map back to the original character offsets.

    The offset map is what makes verbatim evidence possible: a match found in
    normalised space can always be reported as the exact broadcast substring.
    """

    text: str
    offsets: tuple[int, ...]
    source_length: int

    def original_span(self, start: int, end: int) -> tuple[int, int] | None:
        """Map a normalised ``[start, end)`` span back to original offsets."""
        if start < 0 or end <= start or start >= len(self.offsets):
            return None
        raw_start = self.offsets[start]
        raw_end = self.offsets[min(end, len(self.offsets)) - 1] + 1
        return raw_start, min(raw_end, self.source_length)


@lru_cache(maxsize=4096)
def script_of(character: str) -> str:
    """Coarse script name derived from the Unicode character name.

    ``unicodedata`` exposes no script property, but names are stable and start
    with the script for the ranges that matter here. Unknown characters fall
    back to ``COMMON``, which selects the conservative (Latin-style) rules.
    """
    try:
        name = unicodedata.name(character)
    except ValueError:
        return "COMMON"
    head = name.split(" ", 1)[0]
    if head in {"CJK", "IDEOGRAPHIC"}:
        return "HAN"
    if head in {"LATIN", "GREEK", "CYRILLIC", "ARMENIAN", "GEORGIAN"}:
        return head
    return head


def dominant_script(text: str) -> str:
    """The script of the majority of letters in ``text``."""
    counts: dict[str, int] = {}
    for character in text:
        if not character.isalpha():
            continue
        script = script_of(character)
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return "COMMON"
    return max(counts, key=lambda key: counts[key])


def uses_word_boundaries(text: str) -> bool:
    """Whether token-boundary matching is meaningful for ``text``."""
    return dominant_script(text) not in NO_WORD_BOUNDARY_SCRIPTS


def marks_are_significant(text: str) -> bool:
    """Whether combining marks carry meaning and must be preserved."""
    return dominant_script(text) in MARK_SIGNIFICANT_SCRIPTS


def normalize(text: str, *, fold_marks: bool | None = None) -> NormalizedText:
    """Normalise ``text`` while tracking original offsets.

    Steps: strip invisible/bidi controls, NFKC-normalise, case-fold, collapse
    punctuation and whitespace to single spaces, and optionally fold combining
    marks.

    ``fold_marks=None`` (the default) decides per script: marks are folded for
    Latin/Greek/Cyrillic (where ``café`` and ``cafe`` should match) and
    preserved for Indic and other mark-significant scripts (where folding would
    change the word).
    """
    source = str(text or "")
    if fold_marks is None:
        fold_marks = not marks_are_significant(source)

    normalized_chars: list[str] = []
    offsets: list[int] = []
    previous_space = True

    for index, character in enumerate(source):
        if is_invisible(character):
            continue
        decomposed = unicodedata.normalize("NFKC", character).casefold()
        if not decomposed:
            continue
        for part in decomposed:
            category = unicodedata.category(part)
            if category.startswith("M"):
                if fold_marks:
                    continue
                normalized_chars.append(part)
                offsets.append(index)
                previous_space = False
                continue
            if part.isspace() or category in _SEPARATOR_CATEGORIES:
                if not previous_space:
                    normalized_chars.append(" ")
                    offsets.append(index)
                    previous_space = True
                continue
            if fold_marks:
                # NFD then drop marks so that e.g. "é" -> "e" without touching
                # scripts where that would be destructive.
                stripped = "".join(
                    piece
                    for piece in unicodedata.normalize("NFD", part)
                    if not unicodedata.combining(piece)
                )
                part = stripped or part
            for piece in part:
                normalized_chars.append(piece)
                offsets.append(index)
                previous_space = False

    while normalized_chars and normalized_chars[-1] == " ":
        normalized_chars.pop()
        offsets.pop()

    return NormalizedText(
        text="".join(normalized_chars),
        offsets=tuple(offsets),
        source_length=len(source),
    )


def normalized_key(text: str, *, fold_marks: bool | None = None) -> str:
    """Just the normalised string, for index keys and equality checks."""
    return normalize(text, fold_marks=fold_marks).text


def is_word_boundary(text: str, position: int) -> bool:
    """Whether ``position`` in normalised ``text`` is a word boundary.

    Boundaries are alphanumeric-adjacency based rather than ``\\b``-based so
    that scripts without ASCII word characters behave correctly.
    """
    if position <= 0 or position >= len(text):
        return True
    return not (text[position - 1].isalnum() and text[position].isalnum())


def spans_word_boundaries(text: str, start: int, end: int) -> bool:
    """Whether ``[start, end)`` is delimited on both sides."""
    return is_word_boundary(text, start) and is_word_boundary(text, end)


def token_set(text: str) -> frozenset[str]:
    """Whitespace tokens of normalised text; empty for no-boundary scripts."""
    normalized = normalize(text).text
    if not uses_word_boundaries(text):
        return frozenset()
    return frozenset(part for part in _WHITESPACE.split(normalized) if part)


def variant_keys(value: str) -> tuple[str, ...]:
    """Distinct normalised forms worth indexing for one surface form.

    Both mark-folded and mark-preserving forms are produced where they differ,
    so a Hindi keyword written with or without nuqta/matras still matches, while
    the meaning-preserving form remains the primary key.
    """
    keys: list[str] = []
    for fold in (None, True, False):
        candidate = normalize(value, fold_marks=fold).text
        if candidate and candidate not in keys:
            keys.append(candidate)
    return tuple(keys)


__all__ = [
    "MARK_SIGNIFICANT_SCRIPTS",
    "NO_WORD_BOUNDARY_SCRIPTS",
    "NormalizedText",
    "dominant_script",
    "is_word_boundary",
    "marks_are_significant",
    "normalize",
    "normalized_key",
    "script_of",
    "spans_word_boundaries",
    "token_set",
    "uses_word_boundaries",
    "variant_keys",
]
