"""Unicode-aware normalisation.

The tests that matter here are the ones asserting what must NOT be stripped:
the existing `app/text.py` folds all combining marks, which is correct for
Latin and destructive for Indic scripts.
"""
from __future__ import annotations

import pytest

from app.observability import safe_extra
from app.services.text_normalization import (
    _INVISIBLE_RANGES,
    dominant_script,
    is_invisible,
    is_word_boundary,
    marks_are_significant,
    normalize,
    normalized_key,
    spans_word_boundaries,
    token_set,
    uses_word_boundaries,
    variant_keys,
)

# --- basic normalisation ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NVIDIA", "nvidia"),
        ("  NVIDIA  ", "nvidia"),
        ("NVIDIA, Inc.", "nvidia inc"),
        ("N.V.I.D.I.A", "n v i d i a"),
        ("NVIDIA—RTX", "nvidia rtx"),
        ("multiple    spaces", "multiple spaces"),
    ],
)
def test_latin_normalisation(raw: str, expected: str) -> None:
    assert normalized_key(raw) == expected


def test_latin_diacritics_are_folded() -> None:
    """'Café' and 'Cafe' should match: a listener transcribes either."""
    assert normalized_key("Café") == normalized_key("Cafe") == "cafe"
    assert normalized_key("Müller") == "muller"


def test_devanagari_matras_are_preserved() -> None:
    """Stripping a matra changes the word; folding here would be a bug."""
    assert marks_are_significant("किताब") is True
    normalized = normalized_key("किताब")
    assert "ि" in normalized, f"matra was destroyed: {normalized!r}"
    assert normalized_key("किताब") != normalized_key("कताब")


def test_indic_scripts_are_detected() -> None:
    assert dominant_script("किताब") == "DEVANAGARI"
    assert dominant_script("এনভিডিয়া") == "BENGALI"
    assert dominant_script("NVIDIA") == "LATIN"


def test_invisible_characters_are_removed() -> None:
    """A zero-width or bidi character must not be able to defeat a keyword.

    Built with chr() rather than literals: a test file containing real bidi
    controls would be the same Trojan Source hazard the module avoids.
    """
    for codepoint in (0x200B, 0x200E, 0x00AD, 0xFEFF, 0x202E, 0x2060):
        spiked = "NV" + chr(codepoint) + "IDIA"
        assert normalized_key(spiked) == "nvidia", f"U+{codepoint:04X} survived"


def test_is_invisible_covers_every_declared_range() -> None:
    for low, high in _INVISIBLE_RANGES:
        assert is_invisible(chr(low)) and is_invisible(chr(high))
    assert is_invisible("a") is False
    assert is_invisible(" ") is False


def test_source_contains_no_bidirectional_controls() -> None:
    """Regression guard for the Trojan Source class of attack."""
    import pathlib

    import app.services.text_normalization as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    offenders = sorted({hex(ord(c)) for c in source if is_invisible(c)})
    assert offenders == [], f"literal invisible characters in source: {offenders}"


def test_full_width_forms_are_folded_by_nfkc() -> None:
    assert normalized_key("ＮＶＩＤＩＡ") == "nvidia"


def test_empty_and_punctuation_only_inputs() -> None:
    assert normalized_key("") == ""
    assert normalized_key("   ") == ""
    assert normalized_key("!!!") == ""


# --- offset mapping -----------------------------------------------------------


def test_offsets_map_back_to_the_original_text() -> None:
    """This is what makes verbatim evidence possible."""
    raw = "They said NVIDIA, Inc. announced it."
    normalized = normalize(raw)
    start = normalized.text.index("nvidia")
    span = normalized.original_span(start, start + len("nvidia"))
    assert span is not None
    assert raw[span[0] : span[1]] == "NVIDIA"


def test_offsets_survive_punctuation_collapse() -> None:
    raw = "Buy   the  new   NVIDIA!"
    normalized = normalize(raw)
    start = normalized.text.index("nvidia")
    span = normalized.original_span(start, start + len("nvidia"))
    assert raw[span[0] : span[1]] == "NVIDIA"


def test_offsets_survive_diacritic_folding() -> None:
    raw = "Visit the Café today"
    normalized = normalize(raw)
    start = normalized.text.index("cafe")
    span = normalized.original_span(start, start + 4)
    assert raw[span[0] : span[1]] == "Café"


def test_offsets_for_devanagari() -> None:
    raw = "यह किताब अच्छी है"
    normalized = normalize(raw)
    needle = normalized_key("किताब")
    start = normalized.text.index(needle)
    span = normalized.original_span(start, start + len(needle))
    assert raw[span[0] : span[1]] == "किताब"


def test_out_of_range_spans_return_none() -> None:
    normalized = normalize("short")
    assert normalized.original_span(-1, 2) is None
    assert normalized.original_span(3, 3) is None
    assert normalized.original_span(99, 100) is None


# --- word boundaries ----------------------------------------------------------


def test_latin_uses_word_boundaries() -> None:
    assert uses_word_boundaries("NVIDIA") is True
    assert is_word_boundary("say nvidia now", 4) is True
    assert is_word_boundary("saynvidianow", 3) is False


def test_no_boundary_scripts_are_recognised() -> None:
    """CJK/Thai have no spaces, so token matching cannot apply."""
    assert uses_word_boundaries("英伟达") is False
    assert uses_word_boundaries("สวัสดี") is False
    assert token_set("英伟达") == frozenset()


def test_spans_word_boundaries_rejects_substring_hits() -> None:
    text = "nvidian pride"
    assert spans_word_boundaries(text, 0, 6) is False  # "nvidia" inside "nvidian"
    assert spans_word_boundaries("nvidia pride", 0, 6) is True


def test_token_set_for_latin() -> None:
    assert token_set("NVIDIA RTX cards") == {"nvidia", "rtx", "cards"}


# --- variant keys -------------------------------------------------------------


def test_variant_keys_cover_folded_and_unfolded_forms() -> None:
    keys = variant_keys("Café")
    assert "cafe" in keys
    assert any("é" in key or "é" in key for key in keys)


def test_variant_keys_deduplicate_when_forms_coincide() -> None:
    assert variant_keys("NVIDIA") == ("nvidia",)


def test_variant_keys_for_devanagari_keep_the_meaningful_form() -> None:
    keys = variant_keys("किताब")
    assert any("ि" in key for key in keys)


# --- logging helper regression ------------------------------------------------


def test_safe_extra_renames_reserved_logrecord_attributes() -> None:
    """`created` is a LogRecord attribute; passing it raises KeyError."""
    fields = safe_extra({"created": 3, "name": "x", "station_id": "rb-a", "dropped": None})
    assert fields["field_created"] == 3
    assert fields["field_name"] == "x"
    assert fields["station_id"] == "rb-a"
    assert "created" not in fields
    assert "dropped" not in fields


def test_safe_extra_is_usable_as_logging_extra(caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("ok", extra=safe_extra({"created": 1, "module": "m"}))
    assert caplog.records[-1].message == "ok"
