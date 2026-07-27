"""Combined per-station keyword index.

One index per DISTINCT station, built from every active campaign that
references it — never one index per campaign-station pair. That is what makes
"transcribe once, match against all campaigns" true.

Each entry keeps its full provenance (keyword_id, campaign_id, keyword type,
canonical value, alias, language, content policy), so a single match can be
attributed back to every campaign that asked for it without re-running anything.

Versions are content-addressed: the fingerprint covers the *effective* content,
so renaming a campaign or reordering keywords does not churn every listener,
while adding an alias does.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..pipeline.enums import ALIAS_KINDS, STRICT_ENTITY_KINDS
from ..pipeline.ids import content_fingerprint
from .text_normalization import normalized_key, uses_word_boundaries, variant_keys

logger = logging.getLogger(__name__)

#: Guard against a pathological campaign making one station's index unbounded.
MAX_ALIASES_PER_KEYWORD = 200
MAX_ALIAS_LENGTH = 200


@dataclass(frozen=True)
class KeywordAlias:
    """One surface form a keyword may appear as on air."""

    value: str
    language: str | None = None
    kind: str = "canonical"

    def normalized_forms(self) -> tuple[str, ...]:
        return variant_keys(self.value)


@dataclass(frozen=True)
class KeywordEntry:
    """A campaign keyword as it applies to one station."""

    keyword_id: str
    campaign_id: str
    entity_id: str
    canonical_value: str
    keyword_type: str = "brand"
    match_mode: str = "tokens"
    semantic_matching: bool = False
    semantic_threshold: float = 0.74
    aliases: tuple[KeywordAlias, ...] = ()
    languages: tuple[str, ...] = ()
    content_policy: dict[str, bool] = field(default_factory=dict)

    @property
    def is_strict_entity(self) -> bool:
        """Brand/person/product/organization: no translated equivalents."""
        return self.keyword_type in STRICT_ENTITY_KINDS

    @property
    def requires_word_boundaries(self) -> bool:
        return self.match_mode == "tokens" and uses_word_boundaries(self.canonical_value)

    def surface_forms(self) -> tuple[KeywordAlias, ...]:
        canonical = KeywordAlias(value=self.canonical_value, kind="canonical")
        seen = {canonical.value.casefold()}
        output = [canonical]
        for alias in self.aliases:
            marker = alias.value.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            output.append(alias)
        return tuple(output)

    def fingerprint_parts(self) -> tuple[str, ...]:
        """Everything that changes matching behaviour. Order-insensitive."""
        alias_parts = sorted(
            f"{alias.kind}|{alias.language or ''}|{alias.value}" for alias in self.aliases
        )
        policy_parts = sorted(f"{key}={int(bool(value))}" for key, value in self.content_policy.items())
        return (
            self.keyword_id,
            self.campaign_id,
            self.canonical_value,
            self.keyword_type,
            self.match_mode,
            str(int(self.semantic_matching)),
            f"{self.semantic_threshold:.4f}",
            *alias_parts,
            *sorted(self.languages),
            *policy_parts,
        )


@dataclass(frozen=True)
class IndexTerm:
    """One normalised surface form pointing at the keywords that own it.

    Shared terms map to several keywords: two campaigns tracking "NVIDIA"
    produce one term with two owners, so the matcher scans the text once.
    """

    normalized: str
    display: str
    language: str | None
    kind: str
    keyword_ids: tuple[str, ...]
    requires_word_boundaries: bool


@dataclass(frozen=True)
class StationKeywordIndex:
    """The published, versioned index for one station."""

    station_id: str
    version: int
    fingerprint: str
    entries: tuple[KeywordEntry, ...]
    terms: tuple[IndexTerm, ...]

    @property
    def keyword_count(self) -> int:
        return len({entry.keyword_id for entry in self.entries})

    @property
    def campaign_count(self) -> int:
        return len({entry.campaign_id for entry in self.entries})

    @property
    def alias_count(self) -> int:
        return len(self.terms)

    def entry(self, keyword_id: str) -> KeywordEntry | None:
        for candidate in self.entries:
            if candidate.keyword_id == keyword_id:
                return candidate
        return None

    def entries_for(self, keyword_ids: tuple[str, ...]) -> tuple[KeywordEntry, ...]:
        wanted = set(keyword_ids)
        return tuple(entry for entry in self.entries if entry.keyword_id in wanted)

    def campaigns_for(self, keyword_id: str) -> tuple[str, ...]:
        """Every campaign that registered this keyword for this station."""
        return tuple(
            sorted({entry.campaign_id for entry in self.entries if entry.keyword_id == keyword_id})
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialisable form stored in SQLite and mirrored to S3 config/."""
        return {
            "schema": "radio.keyword-index.v1",
            "station_id": self.station_id,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "keyword_count": self.keyword_count,
            "campaign_count": self.campaign_count,
            "alias_count": self.alias_count,
            "entries": [
                {
                    "keyword_id": entry.keyword_id,
                    "campaign_id": entry.campaign_id,
                    "entity_id": entry.entity_id,
                    "canonical_value": entry.canonical_value,
                    "keyword_type": entry.keyword_type,
                    "match_mode": entry.match_mode,
                    "semantic_matching": entry.semantic_matching,
                    "semantic_threshold": entry.semantic_threshold,
                    "languages": list(entry.languages),
                    "content_policy": dict(entry.content_policy),
                    "aliases": [
                        {"value": alias.value, "language": alias.language, "kind": alias.kind}
                        for alias in entry.aliases
                    ],
                }
                for entry in self.entries
            ],
            "terms": [
                {
                    "normalized": term.normalized,
                    "display": term.display,
                    "language": term.language,
                    "kind": term.kind,
                    "keyword_ids": list(term.keyword_ids),
                    "requires_word_boundaries": term.requires_word_boundaries,
                }
                for term in self.terms
            ],
        }


def _clean_alias(raw: Any) -> KeywordAlias | None:
    """Accept both the legacy flat-string alias form and the structured form."""
    if isinstance(raw, str):
        value = raw.strip()
        return KeywordAlias(value=value[:MAX_ALIAS_LENGTH]) if value else None
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("value") or "").strip()
    if not value:
        return None
    kind = str(raw.get("kind") or "canonical").strip().lower()
    if kind not in ALIAS_KINDS:
        kind = "canonical"
    language = str(raw.get("language") or "").strip().lower() or None
    return KeywordAlias(value=value[:MAX_ALIAS_LENGTH], language=language, kind=kind)


def build_entry(binding: dict[str, Any]) -> KeywordEntry:
    """Build one index entry from a campaign-keyword binding row."""
    raw_aliases = binding.get("aliases") or []
    if isinstance(raw_aliases, str):
        try:
            raw_aliases = json.loads(raw_aliases)
        except (TypeError, ValueError):
            raw_aliases = []
    aliases: list[KeywordAlias] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_aliases[:MAX_ALIASES_PER_KEYWORD]:
        alias = _clean_alias(raw)
        if alias is None:
            continue
        marker = (alias.value.casefold(), alias.kind)
        if marker in seen:
            continue
        seen.add(marker)
        aliases.append(alias)

    languages = tuple(
        sorted(
            {
                str(code).strip().lower()
                for code in (binding.get("languages") or [])
                if str(code).strip()
            }
        )
    )
    policy = binding.get("content_policy") or {}
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except (TypeError, ValueError):
            policy = {}

    return KeywordEntry(
        keyword_id=str(binding["keyword_id"]),
        campaign_id=str(binding["campaign_id"]),
        entity_id=str(binding.get("entity_id") or ""),
        canonical_value=str(binding.get("canonical_value") or binding.get("display_name") or ""),
        keyword_type=str(binding.get("keyword_type") or "brand"),
        match_mode=str(binding.get("match_mode") or "tokens"),
        semantic_matching=bool(binding.get("semantic_matching")),
        semantic_threshold=float(binding.get("semantic_threshold") or 0.74),
        aliases=tuple(aliases),
        languages=languages,
        content_policy={str(key): bool(value) for key, value in dict(policy).items()},
    )


def build_index(
    station_id: str,
    bindings: list[dict[str, Any]],
    *,
    previous_version: int = 0,
    previous_fingerprint: str | None = None,
) -> StationKeywordIndex:
    """Build the combined index for one station.

    The version increments only when the fingerprint changes. A no-op rebuild
    therefore does not force every listener to reload.
    """
    entries = tuple(
        sorted(
            (build_entry(binding) for binding in bindings if binding.get("keyword_id")),
            key=lambda entry: (entry.canonical_value.casefold(), entry.keyword_id, entry.campaign_id),
        )
    )
    entries = tuple(entry for entry in entries if entry.canonical_value)

    fingerprint = content_fingerprint(
        station_id, *[part for entry in entries for part in entry.fingerprint_parts()]
    )
    version = previous_version if fingerprint == previous_fingerprint else previous_version + 1

    # Group surface forms so a term shared by several campaigns is scanned once.
    grouped: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for entry in entries:
        for alias in entry.surface_forms():
            for normalized in alias.normalized_forms():
                if not normalized:
                    continue
                key = (normalized, alias.language, alias.kind)
                bucket = grouped.setdefault(
                    key,
                    {
                        "display": alias.value,
                        "keyword_ids": [],
                        "requires_word_boundaries": entry.requires_word_boundaries,
                    },
                )
                if entry.keyword_id not in bucket["keyword_ids"]:
                    bucket["keyword_ids"].append(entry.keyword_id)
                # If any owner needs substring matching, the term must allow it,
                # otherwise that campaign silently loses matches.
                bucket["requires_word_boundaries"] = (
                    bucket["requires_word_boundaries"] and entry.requires_word_boundaries
                )

    terms = tuple(
        IndexTerm(
            normalized=normalized,
            display=str(bucket["display"]),
            language=language,
            kind=kind,
            keyword_ids=tuple(sorted(bucket["keyword_ids"])),
            requires_word_boundaries=bool(bucket["requires_word_boundaries"]),
        )
        # Longest first: the matcher reports the most specific hit when one term
        # is a prefix of another ("NVIDIA" vs "NVIDIA RTX").
        for (normalized, language, kind), bucket in sorted(
            grouped.items(), key=lambda item: (-len(item[0][0]), item[0][0])
        )
    )

    return StationKeywordIndex(
        station_id=station_id,
        version=version,
        fingerprint=fingerprint,
        entries=entries,
        terms=terms,
    )


def index_from_payload(payload: dict[str, Any]) -> StationKeywordIndex:
    """Rebuild an index from its stored payload (listener/worker side)."""
    entries = tuple(
        KeywordEntry(
            keyword_id=str(item["keyword_id"]),
            campaign_id=str(item["campaign_id"]),
            entity_id=str(item.get("entity_id") or ""),
            canonical_value=str(item["canonical_value"]),
            keyword_type=str(item.get("keyword_type") or "brand"),
            match_mode=str(item.get("match_mode") or "tokens"),
            semantic_matching=bool(item.get("semantic_matching")),
            semantic_threshold=float(item.get("semantic_threshold") or 0.74),
            aliases=tuple(
                KeywordAlias(
                    value=str(alias["value"]),
                    language=alias.get("language"),
                    kind=str(alias.get("kind") or "canonical"),
                )
                for alias in item.get("aliases", [])
            ),
            languages=tuple(item.get("languages") or []),
            content_policy=dict(item.get("content_policy") or {}),
        )
        for item in payload.get("entries", [])
    )
    terms = tuple(
        IndexTerm(
            normalized=str(item["normalized"]),
            display=str(item.get("display") or item["normalized"]),
            language=item.get("language"),
            kind=str(item.get("kind") or "canonical"),
            keyword_ids=tuple(item.get("keyword_ids") or []),
            requires_word_boundaries=bool(item.get("requires_word_boundaries", True)),
        )
        for item in payload.get("terms", [])
    )
    return StationKeywordIndex(
        station_id=str(payload["station_id"]),
        version=int(payload.get("version") or 0),
        fingerprint=str(payload.get("fingerprint") or ""),
        entries=entries,
        terms=terms,
    )


def language_hints_for(index: StationKeywordIndex, station_languages: list[str]) -> list[str]:
    """Language hints for ASR: station languages first, then keyword languages."""
    hints: list[str] = []
    for code in station_languages:
        cleaned = str(code).strip().lower()
        if cleaned and cleaned not in hints:
            hints.append(cleaned)
    for entry in index.entries:
        for code in entry.languages:
            if code and code not in hints:
                hints.append(code)
        for alias in entry.aliases:
            if alias.language and alias.language not in hints:
                hints.append(alias.language)
    return hints[:8]


def confirmation_prompt(index: StationKeywordIndex, *, max_characters: int = 400) -> str:
    """Controlled ASR prompt built only from approved surface forms.

    Deliberately not free text: entries are de-duplicated, length-capped and
    joined with a fixed separator, so a keyword cannot inject arbitrary
    instructions into the decoder (ADR-006 §3).
    """
    if max_characters <= 0:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for entry in index.entries:
        for alias in entry.surface_forms():
            value = alias.value.strip()
            marker = normalized_key(value)
            if not value or not marker or marker in seen or len(value) > 60:
                continue
            seen.add(marker)
            parts.append(value)
    prompt = ""
    for part in parts:
        candidate = f"{prompt}, {part}" if prompt else part
        if len(candidate) > max_characters:
            break
        prompt = candidate
    return prompt


__all__ = [
    "MAX_ALIASES_PER_KEYWORD",
    "IndexTerm",
    "KeywordAlias",
    "KeywordEntry",
    "StationKeywordIndex",
    "build_entry",
    "build_index",
    "confirmation_prompt",
    "index_from_payload",
    "language_hints_for",
]
