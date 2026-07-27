"""Combined-index keyword matching (ADR-010 §5).

One transcript is scanned **once** against the whole station index, and each hit
resolves to every campaign that registered that keyword. Fifty campaigns
tracking "NVIDIA" on the same station cost one scan and one match, not fifty.

Scanning
--------
The index is compiled into an Aho-Corasick automaton, so a scan is O(len(text))
regardless of how many keywords the station carries. A per-term ``str.find``
loop is O(terms x len(text)); at the thousands-of-keywords scale in the capacity
requirement that difference is the whole cost of matching. The automaton is
implemented here in pure Python rather than pulled from a C extension because
the base install has no compiled dependencies and the deployment target is
ARM64 -- and it is built once per keyword-index *version*, then cached, so the
build cost is amortised across every segment of every station using it.

Evidence
--------
Matching happens on normalised text, but every reported match carries the
**verbatim original substring** and its character offsets, recovered through the
offset map in :mod:`app.services.text_normalization`. A user is never shown
normalised text as evidence, and an LLM can never be the reason a mention
exists -- it only ever explains one that the matcher already proved.

Match levels
------------
``exact``/``alias``/``transliteration`` are confirmed directly. ``fuzzy``,
``phonetic`` and ``semantic`` are *candidates*: they set
``requires_confirmation`` and must survive a higher-quality second ASR pass
before they may become a mention. Translated equivalents are refused outright
for brands, people, products and organisations -- "Apple" the company is not
"सेब" the fruit.
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..pipeline.enums import CONFIRMATION_REQUIRED_LEVELS, MatchLevel
from .keyword_index import IndexTerm, KeywordEntry, StationKeywordIndex
from .text_normalization import (
    NormalizedText,
    normalize,
    spans_word_boundaries,
    uses_word_boundaries,
)

logger = logging.getLogger(__name__)

#: Alias kind -> the match level a hit on that surface form produces.
ALIAS_KIND_LEVELS: dict[str, MatchLevel] = {
    "canonical": "exact",
    "native_script": "alias",
    "abbreviation": "alias",
    "asr_variant": "alias",
    "translation": "alias",
    "romanization": "transliteration",
    "phonetic": "phonetic",
}

#: Base confidence per level. Candidate levels start low on purpose: they exist
#: to trigger confirmation, not to stand on their own.
LEVEL_CONFIDENCE: dict[str, float] = {
    "exact": 1.0,
    "alias": 0.95,
    "transliteration": 0.85,
    "fuzzy": 0.55,
    "phonetic": 0.5,
    "semantic": 0.45,
}

#: Fuzzy matching is only attempted for terms at least this long. Below it, an
#: edit distance of one is most of the word, and precision collapses.
MIN_FUZZY_TERM_LENGTH = 6

#: Hard ceiling on fuzzy comparisons per transcript, so a station with a huge
#: index cannot turn one segment into an unbounded amount of work.
MAX_FUZZY_COMPARISONS = 20_000


# --- Aho-Corasick -------------------------------------------------------------


class AhoCorasick:
    """Multi-pattern string matcher over Unicode code points.

    Nodes are stored as parallel lists rather than objects: at tens of thousands
    of states that is a large constant-factor saving in both memory and lookup
    time, and the structure is immutable once built.
    """

    __slots__ = ("_goto", "_fail", "_output", "_pattern_lengths")

    def __init__(self, patterns: Iterable[str]) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[list[int]] = [[]]
        self._pattern_lengths: list[int] = []

        for index, pattern in enumerate(patterns):
            self._pattern_lengths.append(len(pattern))
            if not pattern:
                continue
            node = 0
            for character in pattern:
                nxt = self._goto[node].get(character)
                if nxt is None:
                    nxt = len(self._goto)
                    self._goto.append({})
                    self._fail.append(0)
                    self._output.append([])
                    self._goto[node][character] = nxt
                node = nxt
            self._output[node].append(index)

        self._build_failure_links()

    def _build_failure_links(self) -> None:
        queue: deque[int] = deque()
        for node in self._goto[0].values():
            self._fail[node] = 0
            queue.append(node)
        while queue:
            current = queue.popleft()
            for character, target in self._goto[current].items():
                queue.append(target)
                fallback = self._fail[current]
                while fallback and character not in self._goto[fallback]:
                    fallback = self._fail[fallback]
                self._fail[target] = self._goto[fallback].get(character, 0)
                # Merge suffix outputs so overlapping patterns ("RTX" inside
                # "NVIDIA RTX") are all reported from a single pass.
                self._output[target].extend(self._output[self._fail[target]])

    @property
    def state_count(self) -> int:
        return len(self._goto)

    def find(self, text: str) -> Iterator[tuple[int, int, int]]:
        """Yield ``(start, end, pattern_index)`` for every occurrence."""
        node = 0
        goto = self._goto
        fail = self._fail
        output = self._output
        lengths = self._pattern_lengths
        for position, character in enumerate(text):
            while node and character not in goto[node]:
                node = fail[node]
            node = goto[node].get(character, 0)
            if not output[node]:
                continue
            for pattern_index in output[node]:
                length = lengths[pattern_index]
                yield position + 1 - length, position + 1, pattern_index


# --- compiled index -----------------------------------------------------------


@dataclass(frozen=True)
class CompiledKeywordIndex:
    """A station keyword index prepared for scanning."""

    station_id: str
    version: int
    automaton: AhoCorasick
    terms: tuple[IndexTerm, ...]
    entries_by_keyword: dict[str, tuple[KeywordEntry, ...]]

    @property
    def term_count(self) -> int:
        return len(self.terms)


_COMPILED_CACHE: dict[tuple[str, int, str], CompiledKeywordIndex] = {}
#: Bounded so a long-lived worker cycling through many stations cannot grow
#: without limit; stations are re-compiled on demand if evicted.
_COMPILED_CACHE_LIMIT = 64


def compile_index(index: StationKeywordIndex) -> CompiledKeywordIndex:
    """Compile and cache the automaton for one index version.

    Keyed on the fingerprint as well as the version so that a rebuilt index with
    identical content reuses the automaton, and a content change can never be
    served from a stale cache entry.
    """
    key = (index.station_id, index.version, index.fingerprint)
    cached = _COMPILED_CACHE.get(key)
    if cached is not None:
        return cached

    entries_by_keyword: dict[str, list[KeywordEntry]] = {}
    for entry in index.entries:
        entries_by_keyword.setdefault(entry.keyword_id, []).append(entry)

    compiled = CompiledKeywordIndex(
        station_id=index.station_id,
        version=index.version,
        automaton=AhoCorasick(term.normalized for term in index.terms),
        terms=index.terms,
        entries_by_keyword={
            keyword_id: tuple(items) for keyword_id, items in entries_by_keyword.items()
        },
    )
    if len(_COMPILED_CACHE) >= _COMPILED_CACHE_LIMIT:
        _COMPILED_CACHE.pop(next(iter(_COMPILED_CACHE)))
    _COMPILED_CACHE[key] = compiled
    return compiled


def clear_compiled_cache() -> None:
    _COMPILED_CACHE.clear()


# --- timeline -----------------------------------------------------------------


@dataclass(frozen=True)
class TimelineEntry:
    start_char: int
    end_char: int
    start_ms: int
    end_ms: int


class Timeline:
    """Maps character offsets in the assembled transcript back to milliseconds.

    Built from ASR segments (or words, when a second pass produced them). If no
    timing is available the timeline reports ``None`` rather than guessing:
    an invented timestamp on evidence audio is worse than a missing one.
    """

    def __init__(self, entries: Sequence[TimelineEntry] = ()) -> None:
        self._entries = tuple(entries)

    @classmethod
    def from_segments(cls, segments: Iterable[Any], *, separator: str = " ") -> Timeline:
        """Build from objects exposing ``text``, ``start_ms`` and ``end_ms``."""
        entries: list[TimelineEntry] = []
        cursor = 0
        for segment in segments:
            text = str(getattr(segment, "text", "") or "")
            if not text:
                continue
            start = cursor
            cursor += len(text)
            entries.append(
                TimelineEntry(
                    start_char=start,
                    end_char=cursor,
                    start_ms=int(getattr(segment, "start_ms", 0) or 0),
                    end_ms=int(getattr(segment, "end_ms", 0) or 0),
                )
            )
            cursor += len(separator)
        return cls(entries)

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def span_ms(self, start_char: int, end_char: int) -> tuple[int | None, int | None]:
        """Millisecond span covering ``[start_char, end_char)``."""
        if not self._entries:
            return None, None
        start_ms: int | None = None
        end_ms: int | None = None
        for entry in self._entries:
            if entry.end_char <= start_char or entry.start_char >= end_char:
                continue
            if start_ms is None or entry.start_ms < start_ms:
                start_ms = entry.start_ms
            if end_ms is None or entry.end_ms > end_ms:
                end_ms = entry.end_ms
        return start_ms, end_ms


# --- matches ------------------------------------------------------------------


@dataclass(frozen=True)
class KeywordMatch:
    """One confirmed or candidate keyword hit, with verbatim evidence."""

    keyword_id: str
    campaign_ids: tuple[str, ...]
    canonical_value: str
    matched_text: str
    match_level: MatchLevel
    start_char: int
    end_char: int
    start_ms: int | None = None
    end_ms: int | None = None
    confidence: float = 1.0
    alias_kind: str = "canonical"
    language: str | None = None
    keyword_type: str = "brand"

    @property
    def requires_confirmation(self) -> bool:
        """Candidate levels must survive a pass-B re-decode (ADR-010 §5)."""
        return self.match_level in CONFIRMATION_REQUIRED_LEVELS


@dataclass(frozen=True)
class MatchReport:
    """Everything one transcript scan produced."""

    station_id: str
    keyword_index_version: int
    matches: tuple[KeywordMatch, ...] = ()
    scanned_characters: int = 0

    @property
    def has_matches(self) -> bool:
        return bool(self.matches)

    @property
    def confirmed(self) -> tuple[KeywordMatch, ...]:
        return tuple(match for match in self.matches if not match.requires_confirmation)

    @property
    def candidates(self) -> tuple[KeywordMatch, ...]:
        return tuple(match for match in self.matches if match.requires_confirmation)

    @property
    def keyword_ids(self) -> tuple[str, ...]:
        return tuple(sorted({match.keyword_id for match in self.matches}))

    @property
    def campaign_ids(self) -> tuple[str, ...]:
        """Every campaign this transcript is relevant to. One scan, many owners."""
        return tuple(sorted({cid for match in self.matches for cid in match.campaign_ids}))


# --- matcher ------------------------------------------------------------------


class KeywordMatcher:
    """Scans transcripts against one station's combined index."""

    def __init__(
        self,
        index: StationKeywordIndex,
        *,
        enable_fuzzy: bool = False,
        phonetic_encoder: Any | None = None,
    ) -> None:
        self._index = index
        self._compiled = compile_index(index)
        self._enable_fuzzy = enable_fuzzy
        # No default phonetic encoder ships: an unevaluated one would silently
        # manufacture matches in languages nobody measured. The seam exists so
        # a measured encoder can be injected later (ADR-010).
        self._phonetic_encoder = phonetic_encoder

    @property
    def term_count(self) -> int:
        return self._compiled.term_count

    def match(self, transcript: str, *, timeline: Timeline | None = None) -> MatchReport:
        """Scan ``transcript`` and attribute every hit to its campaigns."""
        normalized = normalize(transcript)
        matches: list[KeywordMatch] = []
        if normalized.text:
            matches.extend(self._scan_exact(normalized, transcript, timeline))
            if self._enable_fuzzy:
                matches.extend(self._scan_fuzzy(normalized, transcript, timeline, matches))

        return MatchReport(
            station_id=self._index.station_id,
            keyword_index_version=self._index.version,
            matches=tuple(_deduplicate(matches)),
            scanned_characters=len(normalized.text),
        )

    # -- exact / alias / transliteration ---------------------------------------

    def _scan_exact(
        self,
        normalized: NormalizedText,
        original: str,
        timeline: Timeline | None,
    ) -> Iterator[KeywordMatch]:
        text = normalized.text
        for start, end, term_index in self._compiled.automaton.find(text):
            term = self._compiled.terms[term_index]
            if term.requires_word_boundaries and not spans_word_boundaries(text, start, end):
                # "cat" must not match inside "concatenate". Only applied where
                # word boundaries exist: CJK and Thai have none, and demanding
                # them there would reject every correct match.
                continue
            span = normalized.original_span(start, end)
            if span is None:
                continue
            yield from self._emit(term, span, original, timeline)

    def _emit(
        self,
        term: IndexTerm,
        span: tuple[int, int],
        original: str,
        timeline: Timeline | None,
    ) -> Iterator[KeywordMatch]:
        start_char, end_char = span
        matched_text = original[start_char:end_char]
        start_ms, end_ms = (
            timeline.span_ms(start_char, end_char) if timeline else (None, None)
        )
        level = ALIAS_KIND_LEVELS.get(term.kind, "alias")

        for keyword_id in term.keyword_ids:
            entries = self._compiled.entries_by_keyword.get(keyword_id, ())
            if not entries:
                continue
            entry = entries[0]
            if not self._alias_kind_allowed(entry, term.kind):
                continue
            yield KeywordMatch(
                keyword_id=keyword_id,
                # Every campaign that registered this keyword for this station.
                campaign_ids=tuple(sorted({item.campaign_id for item in entries})),
                canonical_value=entry.canonical_value,
                matched_text=matched_text,
                match_level=level,
                start_char=start_char,
                end_char=end_char,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=LEVEL_CONFIDENCE.get(level, 0.5),
                alias_kind=term.kind,
                language=term.language,
                keyword_type=entry.keyword_type,
            )

    @staticmethod
    def _alias_kind_allowed(entry: KeywordEntry, alias_kind: str) -> bool:
        """Named entities do not accept translated equivalents.

        Mirrors the rule already enforced in the legacy LLM matcher: "Apple" the
        company is not "सेब" the fruit, and a translation match on a brand is a
        false positive by construction rather than a threshold problem.
        """
        if alias_kind == "translation" and entry.is_strict_entity:
            return False
        return True

    # -- controlled fuzzy ------------------------------------------------------

    def _scan_fuzzy(
        self,
        normalized: NormalizedText,
        original: str,
        timeline: Timeline | None,
        existing: list[KeywordMatch],
    ) -> Iterator[KeywordMatch]:
        """Edit-distance-1 candidates for long terms only.

        Deliberately narrow. Fuzzy matching is how a keyword system starts
        reporting things nobody asked for, so it is off by default, restricted
        to terms long enough that one edit is a small fraction of the word,
        skipped entirely for scripts without word boundaries, hard-capped in
        total work, and every hit it produces still requires confirmation.
        """
        text = normalized.text
        already = {(match.start_char, match.end_char) for match in existing}
        candidates = [
            term
            for term in self._compiled.terms
            if len(term.normalized) >= MIN_FUZZY_TERM_LENGTH and term.requires_word_boundaries
        ]
        if not candidates:
            return

        comparisons = 0
        for token, token_start, token_end in _tokens_with_offsets(text):
            if len(token) < MIN_FUZZY_TERM_LENGTH:
                continue
            for term in candidates:
                if comparisons >= MAX_FUZZY_COMPARISONS:
                    logger.debug(
                        "Fuzzy matching hit its comparison ceiling",
                        extra={"station_id": self._index.station_id},
                    )
                    return
                comparisons += 1
                if abs(len(term.normalized) - len(token)) > 1:
                    continue
                if term.normalized == token:
                    continue  # already reported as an exact hit
                if not _within_edit_distance(token, term.normalized, 1):
                    continue
                span = normalized.original_span(token_start, token_end)
                if span is None or span in already:
                    continue
                for match in self._emit(term, span, original, timeline):
                    yield replace(
                        match,
                        match_level="fuzzy",
                        confidence=LEVEL_CONFIDENCE["fuzzy"],
                    )


# --- helpers ------------------------------------------------------------------


def _tokens_with_offsets(text: str) -> Iterator[tuple[str, int, int]]:
    start: int | None = None
    for index, character in enumerate(text):
        if character.isalnum():
            if start is None:
                start = index
        elif start is not None:
            yield text[start:index], start, index
            start = None
    if start is not None:
        yield text[start:], start, len(text)


def _within_edit_distance(left: str, right: str, maximum: int) -> bool:
    """Whether Levenshtein(left, right) <= maximum, with early exit."""
    if abs(len(left) - len(right)) > maximum:
        return False
    if left == right:
        return True
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        best = i
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > maximum:
            return False
        previous = current
    return previous[-1] <= maximum


def _deduplicate(matches: Iterable[KeywordMatch]) -> list[KeywordMatch]:
    """Keep the strongest match per (keyword, span).

    The automaton reports every overlapping term, so "NVIDIA" and "NVIDIA RTX"
    both fire on the same text. The longest, highest-confidence hit wins; the
    shorter one is dropped so a single utterance is not counted twice.
    """
    ranked = sorted(
        matches,
        key=lambda match: (
            match.keyword_id,
            -(match.end_char - match.start_char),
            -match.confidence,
            match.start_char,
        ),
    )
    kept: list[KeywordMatch] = []
    claimed: dict[str, list[tuple[int, int]]] = {}
    for match in ranked:
        spans = claimed.setdefault(match.keyword_id, [])
        if any(start < match.end_char and match.start_char < end for start, end in spans):
            continue
        spans.append((match.start_char, match.end_char))
        kept.append(match)
    return sorted(kept, key=lambda match: (match.start_char, match.keyword_id))


def build_timeline(segments: Iterable[Any]) -> Timeline:
    return Timeline.from_segments(segments)


def supports_word_boundaries(value: str) -> bool:
    return uses_word_boundaries(value)


__all__ = [
    "ALIAS_KIND_LEVELS",
    "LEVEL_CONFIDENCE",
    "MAX_FUZZY_COMPARISONS",
    "MIN_FUZZY_TERM_LENGTH",
    "AhoCorasick",
    "CompiledKeywordIndex",
    "KeywordMatch",
    "KeywordMatcher",
    "MatchReport",
    "Timeline",
    "TimelineEntry",
    "build_timeline",
    "clear_compiled_cache",
    "compile_index",
    "supports_word_boundaries",
]
