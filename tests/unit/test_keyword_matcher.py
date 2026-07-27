"""Multilingual matching, campaign attribution and evidence fidelity."""
from __future__ import annotations

import pytest

from app.services.keyword_index import build_index
from app.services.keyword_matcher import (
    AhoCorasick,
    KeywordMatcher,
    Timeline,
    TimelineEntry,
    clear_compiled_cache,
    compile_index,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_compiled_cache()
    yield
    clear_compiled_cache()


def binding(
    keyword_id: str,
    campaign_id: str,
    value: str,
    *,
    aliases=None,
    keyword_type: str = "brand",
    match_mode: str = "tokens",
):
    return {
        "keyword_id": keyword_id,
        "campaign_id": campaign_id,
        "entity_id": value.lower(),
        "canonical_value": value,
        "keyword_type": keyword_type,
        "match_mode": match_mode,
        "aliases": aliases or [],
        "languages": [],
        "content_policy": {},
    }


def matcher_for(bindings, **kwargs) -> KeywordMatcher:
    return KeywordMatcher(build_index("rb-station", bindings), **kwargs)


# --- automaton ----------------------------------------------------------------


def test_automaton_reports_overlapping_patterns() -> None:
    automaton = AhoCorasick(["nvidia", "nvidia rtx", "rtx"])
    hits = sorted(automaton.find("buy the nvidia rtx today"))
    assert (8, 14, 0) in hits
    assert (8, 18, 1) in hits
    assert (15, 18, 2) in hits


def test_automaton_handles_an_empty_pattern_set() -> None:
    assert list(AhoCorasick([]).find("anything")) == []


def test_automaton_scan_is_independent_of_pattern_count() -> None:
    """A single pass, whatever the index size -- the point of Aho-Corasick."""
    many = AhoCorasick([f"term{index:05d}" for index in range(5_000)] + ["nvidia"])
    assert [hit[2] for hit in many.find("a nvidia advert")] == [5_000]


# --- attribution --------------------------------------------------------------


def test_one_match_attributes_to_every_campaign_that_asked_for_it() -> None:
    """Three campaigns, one keyword, one station: one scan, one match, three owners."""
    matcher = matcher_for(
        [
            binding("kw-1", "campaign-a", "NVIDIA"),
            binding("kw-1", "campaign-b", "NVIDIA"),
            binding("kw-1", "campaign-c", "NVIDIA"),
        ]
    )
    report = matcher.match("The new NVIDIA card is out.")
    assert len(report.matches) == 1, "the transcript is scanned once, not once per campaign"
    assert report.matches[0].campaign_ids == ("campaign-a", "campaign-b", "campaign-c")
    assert report.campaign_ids == ("campaign-a", "campaign-b", "campaign-c")


def test_distinct_keywords_from_distinct_campaigns_both_match() -> None:
    matcher = matcher_for(
        [
            binding("kw-1", "campaign-a", "NVIDIA"),
            binding("kw-2", "campaign-b", "Amazon"),
        ]
    )
    report = matcher.match("NVIDIA and Amazon announced a deal")
    assert report.keyword_ids == ("kw-1", "kw-2")
    assert report.campaign_ids == ("campaign-a", "campaign-b")


def test_no_match_reports_cleanly() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "NVIDIA")])
    report = matcher.match("today's weather is fine")
    assert not report.has_matches
    assert report.campaign_ids == ()


# --- evidence -----------------------------------------------------------------


def test_evidence_is_the_verbatim_original_not_the_normalised_form() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "NVIDIA")])
    transcript = "Introducing the NVIDIA RTX!"
    report = matcher.match(transcript)
    match = report.matches[0]
    assert match.matched_text == "NVIDIA", "casing must survive normalisation"
    assert transcript[match.start_char : match.end_char] == "NVIDIA"


def test_offsets_survive_punctuation_and_diacritics() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "cafe")])
    transcript = "Visit the Café, today!"
    report = matcher.match(transcript)
    assert report.has_matches
    match = report.matches[0]
    assert transcript[match.start_char : match.end_char] == "Café"


def test_timestamps_come_from_the_timeline() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "NVIDIA")])
    timeline = Timeline(
        [
            TimelineEntry(start_char=0, end_char=10, start_ms=0, end_ms=2000),
            TimelineEntry(start_char=10, end_char=30, start_ms=2000, end_ms=5000),
        ]
    )
    report = matcher.match("Later on,  NVIDIA said something", timeline=timeline)
    match = report.matches[0]
    assert match.start_ms == 2000
    assert match.end_ms == 5000


def test_missing_timing_is_reported_as_none_not_zero() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "NVIDIA")])
    match = matcher.match("NVIDIA news").matches[0]
    assert match.start_ms is None, "an invented timestamp would cut the wrong audio"


# --- multilingual -------------------------------------------------------------


def test_devanagari_native_script_alias_matches() -> None:
    matcher = matcher_for(
        [
            binding(
                "kw-1",
                "c-1",
                "NVIDIA",
                aliases=[{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}],
            )
        ]
    )
    report = matcher.match("आज एनवीडिया ने घोषणा की")
    assert report.has_matches
    match = report.matches[0]
    assert match.matched_text == "एनवीडिया"
    assert match.match_level == "alias"


def test_code_switching_hindi_english_matches_both_forms() -> None:
    matcher = matcher_for(
        [
            binding(
                "kw-1",
                "c-1",
                "NVIDIA",
                aliases=[{"value": "एनवीडिया", "language": "hi", "kind": "native_script"}],
            ),
            binding("kw-2", "c-1", "laptop"),
        ]
    )
    report = matcher.match("नया एनवीडिया laptop बहुत अच्छा है")
    assert report.keyword_ids == ("kw-1", "kw-2")


def test_romanization_is_reported_as_transliteration() -> None:
    matcher = matcher_for(
        [
            binding(
                "kw-1",
                "c-1",
                "एनवीडिया",
                aliases=[{"value": "enviidiya", "language": "hi", "kind": "romanization"}],
            )
        ]
    )
    report = matcher.match("the enviidiya announcement")
    assert report.matches[0].match_level == "transliteration"


def test_cjk_matches_without_word_boundaries() -> None:
    """Chinese has no spaces; demanding word boundaries would reject every hit."""
    matcher = matcher_for([binding("kw-1", "c-1", "英伟达")])
    report = matcher.match("今天英伟达发布了新产品")
    assert report.has_matches
    assert report.matches[0].matched_text == "英伟达"


def test_word_boundaries_are_enforced_for_latin_script() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "cat")])
    assert not matcher.match("the concatenate function").has_matches
    assert matcher.match("the cat sat").has_matches


def test_substring_mode_opts_out_of_word_boundaries() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "cat", match_mode="substring")])
    assert matcher.match("the concatenate function").has_matches


# --- precision rules ----------------------------------------------------------


def test_translated_equivalents_are_refused_for_named_entities() -> None:
    """"Apple" the company is not "सेब" the fruit."""
    matcher = matcher_for(
        [
            binding(
                "kw-1",
                "c-1",
                "Apple",
                keyword_type="brand",
                aliases=[{"value": "सेब", "language": "hi", "kind": "translation"}],
            )
        ]
    )
    assert not matcher.match("मुझे सेब पसंद है").has_matches
    assert matcher.match("Apple announced").has_matches


def test_translations_are_allowed_for_topics() -> None:
    matcher = matcher_for(
        [
            binding(
                "kw-1",
                "c-1",
                "election",
                keyword_type="topic",
                aliases=[{"value": "चुनाव", "language": "hi", "kind": "translation"}],
            )
        ]
    )
    assert matcher.match("आज चुनाव की खबर").has_matches


def test_the_longest_overlapping_hit_wins_per_keyword() -> None:
    matcher = matcher_for(
        [
            binding(
                "kw-1",
                "c-1",
                "NVIDIA",
                aliases=[{"value": "NVIDIA RTX", "kind": "asr_variant"}],
            )
        ]
    )
    report = matcher.match("the NVIDIA RTX launch")
    assert len(report.matches) == 1
    assert report.matches[0].matched_text == "NVIDIA RTX"


# --- confirmation levels ------------------------------------------------------


def test_exact_and_alias_hits_do_not_require_confirmation() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "NVIDIA")])
    match = matcher.match("NVIDIA today").matches[0]
    assert match.match_level == "exact"
    assert not match.requires_confirmation


def test_fuzzy_is_off_by_default() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "Volkswagen")])
    assert not matcher.match("the volkswagon dealership").has_matches


def test_fuzzy_when_enabled_produces_candidates_needing_confirmation() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "Volkswagen")], enable_fuzzy=True)
    report = matcher.match("the volkswagon dealership")
    assert report.has_matches
    match = report.matches[0]
    assert match.match_level == "fuzzy"
    assert match.requires_confirmation, "a fuzzy hit must never stand as a mention alone"
    assert report.confirmed == ()
    assert report.candidates == (match,)


def test_fuzzy_ignores_short_terms_where_one_edit_is_most_of_the_word() -> None:
    matcher = matcher_for([binding("kw-1", "c-1", "Bolt")], enable_fuzzy=True)
    assert not matcher.match("the boat race").has_matches


# --- compilation and caching --------------------------------------------------


def test_compiled_index_is_reused_for_the_same_version() -> None:
    index = build_index("rb-station", [binding("kw-1", "c-1", "NVIDIA")])
    assert compile_index(index) is compile_index(index)


def test_a_content_change_produces_a_new_automaton() -> None:
    first = build_index("rb-station", [binding("kw-1", "c-1", "NVIDIA")])
    second = build_index(
        "rb-station",
        [binding("kw-1", "c-1", "NVIDIA", aliases=[{"value": "एनवीडिया", "kind": "native_script"}])],
        previous_version=first.version,
        previous_fingerprint=first.fingerprint,
    )
    assert second.version == first.version + 1
    assert compile_index(first) is not compile_index(second)


def test_matcher_scales_to_a_large_index() -> None:
    bindings = [
        binding(f"kw-{index}", f"campaign-{index % 50}", f"Brand{index:05d}")
        for index in range(2_000)
    ]
    bindings.append(binding("kw-target", "campaign-x", "NVIDIA"))
    matcher = matcher_for(bindings)
    report = matcher.match("a long transcript mentioning NVIDIA once " * 20)
    assert report.keyword_ids == ("kw-target",)
    assert matcher.term_count > 2_000
