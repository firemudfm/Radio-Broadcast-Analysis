#!/usr/bin/env python3
"""Container health check for the API.

Queries ``/readyz`` on localhost. Stdlib only and no application import: a
health check that imports the app would load configuration, open SQLite and
construct services on every probe, which is both slow and capable of failing
for reasons unrelated to whether the server is up.

Exit 0 = healthy, 1 = unhealthy, as Docker's HEALTHCHECK expects.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 4.0


def main() -> int:
    port = os.environ.get("RADIO_API_PORT", "8788")
    url = f"http://127.0.0.1:{port}/readyz"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            body = json.loads(response.read() or b"{}")
            status = response.status
    except urllib.error.HTTPError as error:
        # /readyz answers 503 with a body when it is alive but not ready. That
        # is a legitimate answer, so read it rather than treating it as a crash.
        try:
            body = json.loads(error.read() or b"{}")
        except (ValueError, OSError):
            body = {}
        print(f"not ready ({error.code}): {body.get('checks', {})}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - any failure to reach it is unhealthy
        print(f"unreachable: {type(error).__name__}", file=sys.stderr)
        return 1

    ready = bool(body.get("ready"))
    if status == 200 and ready:
        return 0
    print(f"not ready ({status}): {body.get('checks', {})}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
