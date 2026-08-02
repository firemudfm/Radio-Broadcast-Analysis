"""Validate the production configuration against the EXACT deployed code.

    python -m app.cli.validate_configuration [--json]

Run inside the freshly built ``radio-api:<commit>`` image, so the Settings model
doing the validating is the one that will actually run. Validating against the
source repository's working tree instead would check the configuration against
whatever happens to be checked out on the host -- which, after a fetch that did
not move the working tree, is not the commit being deployed. A setting the new
commit requires would then pass validation and fail at start-up.

Deliberately does not: start the HTTP server, open a socket, contact AWS, load a
model, or touch the spool. It constructs Settings and prints a summary.

The summary is sanitised. A configuration validator that dumps the environment
is a configuration validator that leaks the audio token secret into a
deployment log, which is a public artefact.

Exit codes:
    0   configuration is valid
    2   configuration is not valid
    64  bad usage
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BAD_USAGE = 64

#: Reported verbatim. Every one is an operational fact an operator needs in the
#: deployment log, and none of them is a secret, a credential, an endpoint, a
#: queue URL or a bucket name.
SAFE_FIELDS = (
    "RADIO_QUEUE_BACKEND",
    "RADIO_MAX_REQUESTED_UNIQUE_STATIONS",
    "RADIO_SEGMENT_STORE",
    "RADIO_MAX_ACTIVE_UNIQUE_STATIONS",
    "RADIO_LISTENER_MAX_SESSIONS",
    "RADIO_LISTENER_SHARD_COUNT",
    "RADIO_LISTENER_SHARD_INDEX",
    "RADIO_LLM_ENABLED",
    "RADIO_SEMANTIC_DISCOVERY_ENABLED",
    "RADIO_INCLUDE_SONG_LYRICS",
    "RADIO_INCLUDE_SPEECH_OVER_MUSIC",
    "RADIO_TRANSCRIBE_UNCERTAIN_AUDIO",
    "RADIO_API_VERSION",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.validate_configuration",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the sanitised summary as JSON instead of key=value lines.",
    )
    return parser


def summarise(settings: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name in SAFE_FIELDS:
        if not hasattr(settings, name):
            continue
        value = getattr(settings, name)
        # Never str() an arbitrary object: a pydantic SecretStr renders as
        # "**********", but a plain str would render its actual value, and one
        # field promoted to a secret later must not start leaking here.
        if value is None or isinstance(value, str | int | float | bool):
            summary[name] = value
        else:
            summary[name] = str(type(value).__name__)
    return summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from app.config import Settings

        settings = Settings()  # type: ignore[call-arg]
    except Exception as error:
        # The message, never the environment. A pydantic ValidationError names
        # the offending field and does not echo its value, which is what makes
        # it safe to print; the environment itself never is.
        print(
            f"CONFIGURATION_INVALID: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return EXIT_INVALID

    summary = summarise(settings)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print("CONFIGURATION_VALID")
        for name in sorted(summary):
            print(f"{name}={summary[name]}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
