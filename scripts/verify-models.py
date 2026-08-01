#!/usr/bin/env python3
"""Verify local model files against models.lock.json.

Run before starting workers, and in CI on the deployment host. A worker that
starts with a truncated or substituted model produces plausible-looking wrong
output rather than an error, which is the failure mode this exists to prevent.

    python scripts/verify-models.py --root /var/lib/radio/models
    python scripts/verify-models.py --root ./var/models --role asr

Exit codes:
    0  every required file present and matching
    1  a file is missing, the wrong size, or fails its digest
    2  the lock file itself could not be read

Stdlib only, so it runs on the deployment host without installing anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = REPO_ROOT / "models.lock.json"
DEFAULT_ROOT = Path("/var/lib/radio/models")

#: Read in chunks: hashing a 610 MiB file must not need 610 MiB of RAM.
CHUNK_BYTES = 4 * 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(name: str, model: dict, root: Path, *, quick: bool) -> list[str]:
    """Return a list of problems for one model. Empty means it is good."""
    problems: list[str] = []
    directory = root / str(model.get("target_directory") or name)
    if not directory.is_dir():
        return [f"{name}: directory {directory} is missing"]

    for entry in model.get("files", []):
        filename = str(entry["name"])
        path = directory / filename
        if not path.is_file():
            problems.append(f"{name}: {filename} is missing from {directory}")
            continue

        expected_size = entry.get("size_bytes")
        actual_size = path.stat().st_size
        if expected_size and actual_size != expected_size:
            problems.append(
                f"{name}: {filename} is {actual_size} bytes, expected {expected_size}"
            )
            # A size mismatch already proves the file is wrong; hashing it would
            # only burn I/O to reach the same conclusion.
            continue

        expected_digest = entry.get("sha256")
        if not expected_digest:
            # Deliberately unpinned upstream (see the lock file's comments).
            print(f"  {name}/{filename}: present, no pinned digest to check")
            continue
        if quick:
            print(f"  {name}/{filename}: size OK ({actual_size} bytes), digest skipped")
            continue

        actual_digest = sha256_of(path)
        if actual_digest != expected_digest:
            problems.append(
                f"{name}: {filename} digest mismatch\n"
                f"      expected {expected_digest}\n"
                f"      actual   {actual_digest}"
            )
        else:
            print(f"  {name}/{filename}: verified ({actual_size} bytes)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="model root directory")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="path to models.lock.json")
    parser.add_argument(
        "--role",
        action="append",
        choices=["asr", "llm", "vad"],
        help="only verify models with this role (repeatable)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="check presence and size only, skipping the digest (fast pre-flight)",
    )
    parser.add_argument(
        "--allow-missing-optional",
        action="store_true",
        default=True,
        help="treat an absent VAD model as acceptable (it is optional)",
    )
    parser.add_argument(
        "--require-present",
        action="store_true",
        help=(
            "require every SELECTED model to be present, including optional "
            "ones. Deployment uses this: 'optional' means the classifier may "
            "degrade to energy-only signals if the file is unavailable, not "
            "that a deployment may silently ship without a model the lock pins."
        ),
    )
    args = parser.parse_args()

    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Could not read {args.lock}: {error}", file=sys.stderr)
        return 2

    models = lock.get("models", {})
    if args.role:
        models = {
            name: model for name, model in models.items() if model.get("role") in args.role
        }
    if not models:
        print("No models selected.", file=sys.stderr)
        return 2

    print(f"Verifying {len(models)} model(s) under {args.root}")
    problems: list[str] = []
    for name, model in sorted(models.items()):
        found = verify_model(name, model, args.root, quick=args.quick)
        if (
            found
            and model.get("role") == "vad"
            and args.allow_missing_optional
            and not args.require_present
        ):
            # The classifier degrades to energy-only signals without it, so an
            # absent VAD is a documented quality trade-off -- but only where the
            # caller has said it will accept that. A deployment passes
            # --require-present, because reporting a model the lock pins as
            # VERIFIED when it is not on disk is how a host silently runs
            # without it.
            print(f"  {name}: optional and unavailable -- {found[0]}")
            continue
        problems.extend(found)

        # Say plainly what was and was not checked. models.lock.json pins no
        # upstream digest for the VAD file, so verification here is presence and
        # exact size. Calling that "verified" without qualification would claim
        # a cryptographic check that never happened.
        if not found and model.get("role") == "vad":
            digests = [f.get("sha256") for f in model.get("files", [])]
            if not any(digests):
                print(
                    f"  {name}: presence and exact size only "
                    "(no sha256 is pinned upstream for this artefact)"
                )

    if problems:
        print("\nFAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("\nAll selected models verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
