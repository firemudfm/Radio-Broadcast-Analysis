#!/usr/bin/env python3
"""Container health check for the API.

Queries ``/healthz`` on localhost. Stdlib only and no application import: a
health check that imports the app would load configuration, open SQLite and
construct services on every probe, which is both slow and capable of failing
for reasons unrelated to whether the server is up.

This probes LIVENESS, not full pipeline readiness, and that distinction is
load-bearing during a staged rollout.

``/readyz`` in ``shared_sqs`` mode is ready only once the planner, listener,
transcription and analysis roles have written heartbeats. Using it as the
container health gate meant an ``api``- or ``core``-stage deployment could
never report healthy on a host configured for the shared pipeline: the workers
it waits for are not part of that stage and are never going to start. The
deployment would time out and roll back a release that was working perfectly.

So this asks the narrower question: is the process alive, serving HTTP,
answering with valid JSON, and able to reach its database? Full shared-pipeline
readiness is still asserted -- by ``/readyz`` itself, which is unchanged, and by
``scripts/smoke-test.sh --stage full``, which runs after the containers are up
and checks every worker role individually.

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
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        # An HTTP error still proves something is listening, but /healthz
        # answering non-200 is not something a healthy container does.
        print(f"unhealthy ({error.code})", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - any failure to reach it is unhealthy
        print(f"unreachable: {type(error).__name__}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"unhealthy: /healthz returned {status}", file=sys.stderr)
        return 1

    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        print("unhealthy: /healthz did not return valid JSON", file=sys.stderr)
        return 1
    if not isinstance(body, dict) or "status" not in body:
        print("unhealthy: /healthz body is not a health document", file=sys.stderr)
        return 1

    # The database is the one dependency this process cannot serve without.
    # S3 and the LLM are reported separately and are deliberately NOT fatal
    # here: an S3 outage does not mean this container should be replaced, and
    # `llm` is legitimately "disabled" in every stage that does not run it.
    database = body.get("database")
    if database != "ok":
        print(f"unhealthy: database is {database!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
