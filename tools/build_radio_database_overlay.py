"""Build Radio Browser overlay data files from a radio-database zip snapshot.

The radio-database repository (a curated Radio Browser contribution repo) ships
``src/**/*.json`` files, each containing a JSON array of either station
override records or deletion records (``{"stationuuid": ..., "delete": true}``).

This tool reads such a snapshot zip (e.g. the GitHub "Download ZIP" artifact,
``radio-database-main.zip``) entirely in memory and produces two deterministic
data files:

- ``radio_database_overrides.json``: normalized station override records,
  sorted by ``station_uuid``.
- ``radio_database_deletions.json``: sorted, deduplicated list of deleted
  station UUIDs.

Validation is strict for fields that matter downstream (stream ``url``,
``countrycode``, delete markers, duplicate override UUIDs). Two known
dirty-data cases in the upstream snapshot are handled leniently with a
warning instead of a hard error, so a full snapshot still builds:

- an invalid ``homepage``/``favicon`` URL is dropped to ``""``;
- a ``stationuuid`` that is not a well-formed UUID is kept as its lowercased
  raw string.

Usage:
    python tools/build_radio_database_overlay.py \
        --zip <path-to-radio-database-main.zip> --out-dir app/data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

OVERRIDES_FILENAME = "radio_database_overrides.json"
DELETIONS_FILENAME = "radio_database_deletions.json"

_ARCHIVE_NAME_PREFIX = "radio-database-"
_ALLOWED_URL_SCHEMES = ("http", "https")


@dataclass
class OverlayData:
    """Parsed and normalized contents of a radio-database snapshot."""

    overrides: list[dict[str, Any]]
    deleted_station_uuids: list[str]
    source_commit: str | None


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def validate_entry_name(name: str) -> None:
    """Reject zip entry names that could escape an extraction root (zip-slip).

    Entries are only ever read in memory, but unsafe names are still treated
    as a corrupt or hostile archive and rejected outright.
    """
    if name.startswith(("/", "\\")):
        raise ValueError(f"unsafe zip entry (absolute path): {name!r}")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise ValueError(f"unsafe zip entry (drive letter): {name!r}")
    posix_name = name.replace("\\", "/")
    normalized = posixpath.normpath(posix_name)
    if ".." in posix_name.split("/") or ".." in normalized.split("/"):
        raise ValueError(f"unsafe zip entry (path traversal): {name!r}")


def _is_source_json(name: str) -> bool:
    """Match entries under any ``src/`` directory with a ``.json`` suffix."""
    if not name.endswith(".json"):
        return False
    return "src" in name.split("/")[:-1]


def _parse_station_uuid(value: Any, entry: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{entry}: missing or empty stationuuid")
    text = value.strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        _warn(f"{entry}: stationuuid {text!r} is not a valid UUID; keeping raw value")
        return text.lower()


def _validate_delete_marker(value: Any, entry: str) -> None:
    if value is True:
        return
    if isinstance(value, str) and value.strip().lower() == "true":
        return
    raise ValueError(f"{entry}: unsupported delete value {value!r}")


def _parse_url(value: Any, *, field_name: str, entry: str, allow_empty: bool, lenient: bool) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{entry}: {field_name} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{entry}: {field_name} must not be empty")
    parsed = urlparse(text)
    if parsed.scheme in _ALLOWED_URL_SCHEMES and parsed.hostname:
        return text
    if lenient:
        _warn(f"{entry}: dropping invalid {field_name} URL {text!r}")
        return ""
    raise ValueError(f"{entry}: {field_name} {text!r} is not a valid http(s) URL")


def _parse_country_code(value: Any, entry: str) -> str:
    if not isinstance(value, str) or len(value) != 2 or not value.isascii() or not value.isalpha():
        raise ValueError(f"{entry}: countrycode {value!r} must be exactly 2 ASCII letters")
    return value.upper()


def _split_csv(value: Any, *, field_name: str, entry: str, lowercase: bool) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        raise ValueError(f"{entry}: {field_name} must be a string, got {type(value).__name__}")
    items: list[str] = []
    for chunk in value.split(","):
        item = chunk.strip()
        if item:
            items.append(item.lower() if lowercase else item)
    return items


def _parse_iso_3166_2(value: Any, entry: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{entry}: iso_3166_2 must be a string or null, got {type(value).__name__}")
    text = value.strip()
    return text or None


def _parse_coordinate(value: Any, field_name: str, entry: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{entry}: {field_name} must be a number or null, got {value!r}")
    return float(value)


def _parse_override(record: dict[str, Any], entry: str) -> dict[str, Any]:
    station_uuid = _parse_station_uuid(record.get("stationuuid"), entry)
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{entry}: override {station_uuid} has a missing or empty name")
    return {
        "station_uuid": station_uuid,
        "name": name,
        "url": _parse_url(
            record.get("url"), field_name="url", entry=entry, allow_empty=False, lenient=False
        ),
        "homepage": _parse_url(
            record.get("homepage"), field_name="homepage", entry=entry, allow_empty=True, lenient=True
        ),
        "favicon": _parse_url(
            record.get("favicon"), field_name="favicon", entry=entry, allow_empty=True, lenient=True
        ),
        "country_code": _parse_country_code(record.get("countrycode"), entry),
        "language_codes": _split_csv(
            record.get("languagecodes"), field_name="languagecodes", entry=entry, lowercase=True
        ),
        "tags": _split_csv(record.get("tags"), field_name="tags", entry=entry, lowercase=False),
        "iso_3166_2": _parse_iso_3166_2(record.get("iso_3166_2"), entry),
        "geo_lat": _parse_coordinate(record.get("geo_lat"), "geo_lat", entry),
        "geo_long": _parse_coordinate(record.get("geo_long"), "geo_long", entry),
    }


def _detect_source_commit(top_level_names: set[str]) -> str | None:
    """Derive a ref/commit hint from the archive's single top-level folder.

    GitHub snapshot zips wrap everything in ``<repo>-<ref>/``; for
    ``radio-database-main.zip`` that yields ``main``. There is no true commit
    marker inside the zip, so this is best-effort and otherwise ``None``.
    """
    if len(top_level_names) != 1:
        return None
    (name,) = top_level_names
    if name.startswith(_ARCHIVE_NAME_PREFIX):
        suffix = name[len(_ARCHIVE_NAME_PREFIX) :]
        return suffix or None
    return None


def load_overlay(zip_path: Path) -> OverlayData:
    """Read a radio-database snapshot zip and return normalized overlay data."""
    overrides: dict[str, dict[str, Any]] = {}
    deletions: set[str] = set()
    top_level_names: set[str] = set()

    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for info in infos:
            validate_entry_name(info.filename)
            top_level_names.add(info.filename.split("/", 1)[0])
        for info in infos:
            if info.is_dir() or not _is_source_json(info.filename):
                continue
            raw = zf.read(info)
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{info.filename}: invalid JSON ({exc})") from exc
            if not isinstance(payload, list):
                raise ValueError(f"{info.filename}: expected a JSON array of records")
            for index, record in enumerate(payload):
                if not isinstance(record, dict):
                    raise ValueError(f"{info.filename}: record {index} is not a JSON object")
                entry = f"{info.filename} (record {index})"
                if "delete" in record:
                    _validate_delete_marker(record["delete"], entry)
                    deletions.add(_parse_station_uuid(record.get("stationuuid"), entry))
                else:
                    parsed = _parse_override(record, entry)
                    key = parsed["station_uuid"]
                    if key in overrides:
                        raise ValueError(
                            f"{entry}: duplicate override stationuuid {key}"
                        )
                    overrides[key] = parsed

    return OverlayData(
        overrides=[overrides[key] for key in sorted(overrides)],
        deleted_station_uuids=sorted(deletions),
        source_commit=_detect_source_commit(top_level_names),
    )


def build_metadata(zip_path: Path, data: OverlayData, generated_at: datetime) -> dict[str, Any]:
    return {
        "source_archive": zip_path.name,
        "source_commit": data.source_commit,
        "generated_at_utc": generated_at.isoformat(),
        "override_count": len(data.overrides),
        "deletion_count": len(data.deleted_station_uuids),
        "source_zip_sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def write_outputs(data: OverlayData, zip_path: Path, out_dir: Path) -> tuple[Path, Path]:
    """Write both overlay files into ``out_dir`` and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = build_metadata(zip_path, data, datetime.now(timezone.utc))
    overrides_path = out_dir / OVERRIDES_FILENAME
    deletions_path = out_dir / DELETIONS_FILENAME
    _write_json(overrides_path, {"metadata": metadata, "overrides": data.overrides})
    _write_json(
        deletions_path,
        {"metadata": metadata, "deleted_station_uuids": data.deleted_station_uuids},
    )
    return overrides_path, deletions_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Radio Browser overlay data files from a radio-database zip snapshot."
    )
    parser.add_argument(
        "--zip",
        dest="zip_path",
        required=True,
        type=Path,
        help="Path to the radio-database snapshot zip (e.g. radio-database-main.zip).",
    )
    parser.add_argument(
        "--out-dir",
        dest="out_dir",
        required=True,
        type=Path,
        help="Directory to write the overlay JSON files into.",
    )
    args = parser.parse_args(argv)

    data = load_overlay(args.zip_path)
    overrides_path, deletions_path = write_outputs(data, args.zip_path, args.out_dir)
    print(f"overrides: {len(data.overrides)} -> {overrides_path}")
    print(f"deletions: {len(data.deleted_station_uuids)} -> {deletions_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
