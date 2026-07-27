#!/usr/bin/env python3
"""Download the pinned model files listed in models.lock.json.

Explicitly invoked, never automatic. Container start-up must not pull ~1.1 GB
from a third party: it would make every deploy depend on that host being up,
and it would silently change what a "restart" means.

    python scripts/download-models.py --root /var/lib/radio/models
    python scripts/download-models.py --root ./var/models --role asr
    python scripts/download-models.py --root ./var/models --dry-run

Every download is verified against the pinned digest before being moved into
place, and files are written to a temporary name and renamed, so an interrupted
run can never leave a half-written model that looks complete.

Stdlib only: this runs on a bare deployment host before any dependency is
installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = REPO_ROOT / "models.lock.json"
DEFAULT_ROOT = Path("/var/lib/radio/models")

CHUNK_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 120

USER_AGENT = "radio-broadcast-analysis/model-downloader"


def build_url(model: dict, entry: dict) -> str:
    """Resolve a provider-specific download URL, pinned to the revision."""
    provider = str(model.get("provider"))
    repository = str(model["repository"])
    revision = str(model["revision"])
    path = str(entry.get("source_path") or entry["name"])

    if provider == "huggingface":
        return f"https://huggingface.co/{repository}/resolve/{revision}/{path}"
    if provider == "github":
        return f"https://raw.githubusercontent.com/{repository}/{revision}/{path}"
    raise ValueError(f"Unsupported provider {provider!r}")


def download(url: str, destination: Path, *, expected_sha256: str | None) -> None:
    """Fetch to a temporary file, verify, then rename into place atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    handle, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".part"
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            with os.fdopen(handle, "wb") as stream:
                while chunk := response.read(CHUNK_BYTES):
                    stream.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    print(f"\r    {total / 1_048_576:8.1f} MiB", end="", flush=True)
                stream.flush()
                os.fsync(stream.fileno())
        print()

        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256:
            raise ValueError(
                f"digest mismatch\n      expected {expected_sha256}\n      actual   {actual}"
            )
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--role", action="append", choices=["asr", "llm", "vad"], help="repeatable"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-download files that already exist"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be fetched, download nothing"
    )
    args = parser.parse_args()

    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Could not read {args.lock}: {error}", file=sys.stderr)
        return 2

    models = lock.get("models", {})
    if args.role:
        models = {n: m for n, m in models.items() if m.get("role") in args.role}
    if not models:
        print("No models selected.", file=sys.stderr)
        return 2

    required = sum(
        int(entry.get("size_bytes") or 0)
        for model in models.values()
        for entry in model.get("files", [])
    )
    print(f"Selected {len(models)} model(s), ~{required / 1_048_576:.0f} MiB")
    if not args.dry_run:
        _warn_if_disk_is_tight(args.root, required)

    failures: list[str] = []
    for name, model in sorted(models.items()):
        directory = args.root / str(model.get("target_directory") or name)
        print(f"\n{name}  ({model['repository']} @ {model['revision'][:12]})")
        print(f"  license: {model.get('license', 'unknown')}  ->  {directory}")

        for entry in model.get("files", []):
            destination = directory / str(entry["name"])
            if destination.is_file() and not args.force:
                print(f"  {entry['name']}: already present, skipping")
                continue
            try:
                url = build_url(model, entry)
            except ValueError as error:
                failures.append(f"{name}/{entry['name']}: {error}")
                continue
            if args.dry_run:
                print(f"  {entry['name']}: would fetch {url}")
                continue
            print(f"  {entry['name']}: fetching")
            try:
                download(url, destination, expected_sha256=entry.get("sha256"))
            except (OSError, ValueError, urllib.error.URLError) as error:
                failures.append(f"{name}/{entry['name']}: {error}")
                print(f"    FAILED: {error}", file=sys.stderr)

    if failures:
        print("\nFAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run complete; nothing was written.")
        return 0

    print("\nDownload complete. Verify before starting workers:")
    print(f"  python scripts/verify-models.py --root {args.root}")
    return 0


def _warn_if_disk_is_tight(root: Path, required_bytes: int) -> None:
    """A part-written model is worse than a refused download."""
    probe = root if root.exists() else root.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return
    # 20% headroom: the spool shares this filesystem in the default layout.
    if usage.free < required_bytes * 1.2:
        print(
            f"  WARNING: {usage.free / 1_048_576:.0f} MiB free at {probe}, "
            f"~{required_bytes * 1.2 / 1_048_576:.0f} MiB recommended",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
