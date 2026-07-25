"""Tests for tools/build_radio_database_overlay.py."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tools.build_radio_database_overlay import (
    DELETIONS_FILENAME,
    OVERRIDES_FILENAME,
    load_overlay,
    main,
)

HERTZ_UUID = "0e30b79d-3977-4bb0-9e83-a1914cd757d0"
MANGO_UUID = "78012206-1aa1-11e9-a80b-52543be04c81"
DELETE_UUID_A = "11111111-2222-3333-4444-555555555555"
DELETE_UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Optional integration input: put the curated radio-database export next to
# the repo (or point RADIO_DB_ZIP at it) to exercise the real-overlay test.
REAL_ZIP = Path(os.environ.get("RADIO_DB_ZIP", "radio-database-main.zip"))


def _hertz_record(**extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "stationuuid": HERTZ_UUID.upper(),
        "name": "Hertz 87.9 - Campusradio",
        "url": "https://stream.radiohertz.de/hertz-hq.mp3",
        "homepage": "https://www.hertz879.de/",
        "favicon": "",
        "tags": "Campus Radio, Alternative",
        "countrycode": "de",
        "iso_3166_2": "DE-NW",
        "languagecodes": "DE, en,",
        "geo_lat": 52.03,
        "geo_long": 8.53,
    }
    record.update(extra)
    return record


def _mango_record(**extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "stationuuid": MANGO_UUID,
        "name": "MANGORADIO",
        "url": "https://mangoradio.stream.laut.fm/mangoradio",
        "homepage": "https://mangoradio.de/",
        "favicon": "https://mangoradio.de/logo.webp",
        "tags": "music,variety",
        "countrycode": "DE",
        "iso_3166_2": None,
        "languagecodes": "de",
    }
    record.update(extra)
    return record


def _make_zip(tmp_path: Path, files: dict[str, Any], name: str = "radio-database-main.zip") -> Path:
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for entry, payload in files.items():
            data = payload if isinstance(payload, str) else json.dumps(payload)
            zf.writestr(entry, data)
    return zip_path


def _happy_zip(tmp_path: Path) -> Path:
    return _make_zip(
        tmp_path,
        {
            "radio-database-main/src/de/hertz879.json": [_hertz_record()],
            "radio-database-main/src/de/mangoradio.json": [_mango_record()],
            "radio-database-main/src/de/deletions.json": [
                {"stationuuid": DELETE_UUID_A.upper(), "delete": True},
                {"stationuuid": DELETE_UUID_B, "delete": "TRUE"},
            ],
        },
    )


def _build(zip_path: Path, out_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    exit_code = main(["--zip", str(zip_path), "--out-dir", str(out_dir)])
    assert exit_code == 0
    overrides_doc = json.loads((out_dir / OVERRIDES_FILENAME).read_text(encoding="utf-8"))
    deletions_doc = json.loads((out_dir / DELETIONS_FILENAME).read_text(encoding="utf-8"))
    return overrides_doc, deletions_doc


def test_happy_path(tmp_path: Path) -> None:
    zip_path = _happy_zip(tmp_path)
    overrides_doc, deletions_doc = _build(zip_path, tmp_path / "out")

    metadata = overrides_doc["metadata"]
    assert metadata["override_count"] == 2
    assert metadata["deletion_count"] == 2
    assert metadata["source_archive"] == "radio-database-main.zip"
    assert metadata["source_commit"] == "main"
    assert len(metadata["source_zip_sha256"]) == 64
    assert deletions_doc["metadata"]["override_count"] == 2
    assert deletions_doc["metadata"]["deletion_count"] == 2

    overrides = overrides_doc["overrides"]
    assert [record["station_uuid"] for record in overrides] == [HERTZ_UUID, MANGO_UUID]

    hertz = overrides[0]
    assert hertz["station_uuid"] == HERTZ_UUID  # lowercased from uppercase input
    assert hertz["country_code"] == "DE"  # uppercased from lowercase input
    assert hertz["language_codes"] == ["de", "en"]  # lowercased, empties dropped
    assert hertz["tags"] == ["Campus Radio", "Alternative"]  # original case kept
    assert hertz["favicon"] == ""
    assert hertz["geo_lat"] == pytest.approx(52.03)

    mango = overrides[1]
    assert mango["name"] == "MANGORADIO"
    assert mango["iso_3166_2"] is None
    assert mango["geo_lat"] is None
    assert mango["geo_long"] is None

    assert deletions_doc["deleted_station_uuids"] == sorted([DELETE_UUID_A, DELETE_UUID_B])


def test_duplicate_override_uuid_raises(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path,
        {
            "radio-database-main/src/de/one.json": [_hertz_record()],
            "radio-database-main/src/de/two.json": [_hertz_record(name="Hertz duplicate")],
        },
    )
    with pytest.raises(ValueError, match=HERTZ_UUID):
        load_overlay(zip_path)


@pytest.mark.parametrize("bad_url", ["ftp://example.com/stream", "notaurl"])
def test_malformed_url_raises(tmp_path: Path, bad_url: str) -> None:
    zip_path = _make_zip(
        tmp_path,
        {"radio-database-main/src/de/bad.json": [_hertz_record(url=bad_url)]},
    )
    with pytest.raises(ValueError, match="url"):
        load_overlay(zip_path)


def test_invalid_delete_value_raises(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path,
        {
            "radio-database-main/src/de/deletions.json": [
                {"stationuuid": DELETE_UUID_A, "delete": "yes"}
            ]
        },
    )
    with pytest.raises(ValueError, match="delete"):
        load_overlay(zip_path)


def test_zip_slip_entry_raises(tmp_path: Path) -> None:
    evil_entry = "radio-database-main/src/../../evil.json"
    zip_path = _make_zip(
        tmp_path,
        {
            "radio-database-main/src/de/hertz879.json": [_hertz_record()],
            evil_entry: [],
        },
    )
    with pytest.raises(ValueError, match="evil.json"):
        load_overlay(zip_path)


def test_deterministic_output(tmp_path: Path) -> None:
    zip_path = _happy_zip(tmp_path)
    first_overrides, first_deletions = _build(zip_path, tmp_path / "out1")
    second_overrides, second_deletions = _build(zip_path, tmp_path / "out2")

    assert first_overrides["overrides"] == second_overrides["overrides"]
    assert first_deletions["deleted_station_uuids"] == second_deletions["deleted_station_uuids"]
    for key in ("override_count", "deletion_count", "source_archive", "source_commit", "source_zip_sha256"):
        assert first_overrides["metadata"][key] == second_overrides["metadata"][key]
        assert first_deletions["metadata"][key] == second_deletions["metadata"][key]


def test_real_snapshot_integration(tmp_path: Path) -> None:
    if not REAL_ZIP.exists():
        pytest.skip(f"real snapshot not available at {REAL_ZIP}")

    overrides_doc, deletions_doc = _build(REAL_ZIP, tmp_path / "out")

    assert overrides_doc["metadata"]["override_count"] == 419
    assert deletions_doc["metadata"]["deletion_count"] == 354
    assert len(overrides_doc["overrides"]) == 419
    assert len(deletions_doc["deleted_station_uuids"]) == 354

    by_uuid = {record["station_uuid"]: record for record in overrides_doc["overrides"]}
    assert HERTZ_UUID in by_uuid
    assert MANGO_UUID in by_uuid
    assert "Hertz 87.9" in by_uuid[HERTZ_UUID]["name"]
