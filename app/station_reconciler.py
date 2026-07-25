"""Root-owned station reconciler.

FastAPI (radio user) only queues station jobs in SQLite; this narrow daemon,
installed as radio-station-reconciler.service (root), is the only component
that touches systemd or writes /etc/radio-pipeline configuration. It processes
one job at a time, is idempotent, never stops legacy-pinned stations, and
never resets existing automation state.

Run: python -m app.station_reconciler [--once]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings, get_settings
from .db import Database
from .db_catalog import CatalogStore
from .services.net_safety import MAX_REDIRECTS, NetSafetyError, validate_public_http_url
from .services.radio_browser import RadioBrowserClient, RadioBrowserError

log = logging.getLogger("station-reconciler")

_STATION_ID_RE = re.compile(r"^rb-[0-9a-f-]{8,64}$")
_PIPELINE_UNITS = ("radio-capture@{sid}", "radio-uploader@{sid}", "radio-pipeline-worker@{sid}")

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess]


class CapacityLimitReached(RuntimeError):
    """Activation refused because every active slot is taken.

    Not a station failure: the station stays in pending_capacity so the
    promotion pass can start it once a slot frees.
    """


def _default_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    log.debug("run: %s", shlex.join(command))
    return subprocess.run(  # noqa: S603 - fixed binaries, validated args
        command, capture_output=True, text=True, timeout=timeout, check=False
    )


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        store: CatalogStore,
        radio_browser: RadioBrowserClient,
        *,
        runner: CommandRunner | None = None,
        stations_dir: Path | None = None,
        automation_dir: Path | None = None,
        template_station_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = radio_browser
        self._run = runner or _default_runner
        self._stations_dir = stations_dir or settings.RADIO_STATION_CONFIG_DIR
        self._automation_dir = automation_dir or Path("/etc/radio-pipeline/automation")
        self._template_station_id = template_station_id or (
            settings.RADIO_LEGACY_PINNED_STATION_IDS[0]
            if settings.RADIO_LEGACY_PINNED_STATION_IDS
            else "hertz879"
        )

    # -- main loop -----------------------------------------------------------

    def run_forever(self) -> None:
        log.info("station reconciler started (poll=%ss)", self._settings.RADIO_RECONCILER_POLL_SECONDS)
        while True:
            try:
                worked = self.run_once()
            except Exception:
                log.exception("reconciler cycle failed")
                worked = False
            if not worked:
                time.sleep(self._settings.RADIO_RECONCILER_POLL_SECONDS)

    def run_once(self) -> bool:
        """Process due stops, capacity promotions, and at most one job."""
        self._schedule_due_stops()
        self._promote_pending_capacity()
        job = self._store.claim_next_job()
        if job is None:
            return False
        record = self._store.managed_station(job["managed_station_id"])
        if record is None:
            self._store.finish_job(job["id"], status="failed", error="Managed station vanished")
            return True
        if job["action"] == "activate" and record["desired_state"] != "active":
            # The station was wound down while this job sat in the queue;
            # starting it now would resurrect an unwanted pipeline.
            self._store.finish_job(
                job["id"], status="failed", error="Station no longer wants to run"
            )
            return True
        log.info(
            json.dumps(
                {
                    "event": "job_start",
                    "job_id": job["id"],
                    "action": job["action"],
                    "station_uuid": record["station_uuid"],
                    "local_station_id": record["local_station_id"],
                }
            )
        )
        try:
            if job["action"] in {"probe", "reprobe"}:
                self._do_probe(record, activate_after=record["desired_state"] == "active")
            elif job["action"] == "activate":
                self._do_probe(record, activate_after=True)
            elif job["action"] == "stop":
                self._do_stop(record)
            self._store.finish_job(job["id"], status="completed")
        except CapacityLimitReached as error:
            # Keep the pending_capacity state set by _do_activate; overwriting
            # it with failed_probe would hide the station from promotion.
            log.info("job %s deferred: %s", job["id"], error)
            self._store.finish_job(job["id"], status="failed", error=str(error)[:500])
        except Exception as error:  # noqa: BLE001 - jobs fail closed with a recorded error
            log.exception("job %s failed", job["id"])
            self._store.finish_job(job["id"], status="failed", error=str(error)[:500])
            if job["action"] == "stop" and record["legacy_pinned"]:
                # A refused stop on a pinned station must not alter its state.
                pass
            else:
                self._store.set_station_state(
                    record["id"],
                    actual_state="failed_probe" if job["action"] != "stop" else "degraded",
                    last_error=str(error)[:500],
                )
        return True

    # -- probe -----------------------------------------------------------------

    def _resolve_stream_url(self, record: dict[str, Any]) -> str:
        if record["station_uuid"].startswith("legacy-"):
            raise RuntimeError("Legacy stations are managed outside the reconciler")
        resolved = self._client.resolve_url(record["station_uuid"])
        url = str(resolved.get("url") or "").strip()
        if not url:
            raise RuntimeError("Radio Browser returned no playback URL")
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            target = validate_public_http_url(current)
            probe = self._run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}",
                 "--max-time", "8", "--no-location", target.url],
                12.0,
            )
            parts = (probe.stdout or "").strip().split(" ", 1)
            code = int(parts[0] or 0) if parts and parts[0].isdigit() else 0
            redirect = parts[1].strip() if len(parts) > 1 else ""
            if code in {301, 302, 303, 307, 308} and redirect:
                current = redirect
                continue
            return target.url
        raise NetSafetyError("Too many redirects while resolving the stream")

    def _do_probe(self, record: dict[str, Any], *, activate_after: bool) -> None:
        self._store.set_station_state(record["id"], actual_state="probing")
        stream_url = self._resolve_stream_url(record)
        probe_seconds = self._settings.RADIO_PROBE_SECONDS
        # ffprobe has no "-t" option (that is ffmpeg-only); read time is bounded
        # by -analyzeduration (microseconds) plus the subprocess timeout below.
        command = [
            "runuser", "-u", "radio", "--",
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-select_streams", "a:0",
            "-analyzeduration", "10M",
            "-user_agent", self._settings.RADIO_BROWSER_USER_AGENT,
            "-i", stream_url,
        ]
        try:
            completed = self._run(command, float(probe_seconds + 10))
        except subprocess.TimeoutExpired as error:
            self._record_probe_failure(record, f"ffprobe timed out after {probe_seconds}s")
            raise RuntimeError("Probe timed out") from error
        if completed.returncode != 0:
            detail = (completed.stderr or "ffprobe failed").strip()[:300]
            self._record_probe_failure(record, detail)
            raise RuntimeError(f"Probe failed: {detail}")
        try:
            payload = json.loads(completed.stdout or "{}")
            streams = payload.get("streams") or []
        except json.JSONDecodeError:
            streams = []
        if not streams:
            self._record_probe_failure(record, "No audio stream found (empty/HTML/non-audio)")
            raise RuntimeError("No audio stream found")
        stream = streams[0]
        result = {
            "status": "ok",
            "codec": str(stream.get("codec_name") or "") or None,
            "sample_rate": int(stream.get("sample_rate") or 0) or None,
            "channels": int(stream.get("channels") or 0) or None,
            "duration_seconds": float(stream.get("duration") or 0.0) or None,
            "final_hostname": validate_public_http_url(stream_url).hostname,
            "redirect_chain": [],
            "error": None,
        }
        self._store.record_probe_result(record["id"], result)
        self._store.set_station_state(
            record["id"],
            probe_status="ok",
            stream_url_resolved=stream_url,
            last_error=None,
            actual_state="available" if not activate_after else "activating",
        )
        if activate_after:
            self._do_activate(record, stream_url)

    def _record_probe_failure(self, record: dict[str, Any], detail: str) -> None:
        self._store.record_probe_result(
            record["id"],
            {"status": "failed", "error": detail, "redirect_chain": []},
        )
        self._store.set_station_state(
            record["id"], probe_status="failed", actual_state="failed_probe", last_error=detail
        )

    # -- activate ------------------------------------------------------------------

    def _do_activate(self, record: dict[str, Any], stream_url: str) -> None:
        station_id = record["local_station_id"]
        if not _STATION_ID_RE.match(station_id):
            raise RuntimeError(f"Refusing unsafe local station id: {station_id!r}")
        capacity_used = self._store.active_station_count()
        if capacity_used > self._settings.RADIO_MAX_ACTIVE_STATIONS:
            self._store.set_station_state(record["id"], actual_state="pending_capacity")
            raise CapacityLimitReached("Active station limit reached at activation time")
        self._write_station_env(record, stream_url)
        self._write_automation_env(record)
        for template in _PIPELINE_UNITS:
            unit = template.format(sid=station_id)
            completed = self._run(["systemctl", "enable", "--now", unit], 60.0)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"systemctl enable --now {unit} failed: {(completed.stderr or '').strip()[:200]}"
                )
        for template in _PIPELINE_UNITS:
            unit = template.format(sid=station_id)
            completed = self._run(["systemctl", "is-active", "--quiet", unit], 30.0)
            if completed.returncode != 0:
                self._store.set_station_state(
                    record["id"], actual_state="degraded",
                    last_error=f"{unit} did not become active",
                )
                raise RuntimeError(f"{unit} did not become active")
        self._store.set_station_state(
            record["id"], actual_state="active", last_error=None, stop_after_utc=None
        )
        log.info(json.dumps({"event": "station_active", "station_id": station_id}))

    def _write_station_env(self, record: dict[str, Any], stream_url: str) -> None:
        """Write /etc/radio-pipeline/stations/<id>.env.

        The installed template station (hertz879) is the source of truth for
        non-identity keys so this file always matches what the ingestion
        package expects; identity keys are replaced per station.
        """
        self._stations_dir.mkdir(parents=True, exist_ok=True)
        template_keys: list[tuple[str, str]] = []
        template_path = self._stations_dir / f"{self._template_station_id}.env"
        identity = {"STATION_ID", "STATION_NAME", "STATION_LANGUAGE", "STREAM_URL"}
        if template_path.exists():
            for line in template_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key not in identity:
                    template_keys.append((key, stripped.split("=", 1)[1]))
        language = (record["language_codes"] or ["en"])[0]
        lines = [
            "# Managed by radio-station-reconciler; do not edit by hand.",
            f'STATION_ID="{record["local_station_id"]}"',
            f'STATION_NAME="{_env_escape(record["name"])}"',
            f'STATION_LANGUAGE="{language}"',
            f'STREAM_URL="{stream_url}"',
        ]
        lines.extend(f"{key}={value}" for key, value in template_keys)
        path = self._stations_dir / f"{record['local_station_id']}.env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o640)

    def _write_automation_env(self, record: dict[str, Any]) -> None:
        """Write /etc/radio-pipeline/automation/<id>.env with intelligence and
        sentiment enabled. Never rewrites an existing file (preserves state)."""
        self._automation_dir.mkdir(parents=True, exist_ok=True)
        path = self._automation_dir / f"{record['local_station_id']}.env"
        if path.exists():
            return
        language = (record["language_codes"] or ["en"])[0]
        multilingual = "true" if len(record["language_codes"] or []) != 1 else "false"
        path.write_text(
            "\n".join(
                [
                    "# Managed by radio-station-reconciler; do not edit by hand.",
                    f'STATION_ID="{record["local_station_id"]}"',
                    f'WHISPER_LANGUAGE="{language}"',
                    f'WHISPER_MULTILINGUAL="{multilingual}"',
                    'AUTOMATION_ENABLE_INTELLIGENCE="true"',
                    'AUTOMATION_ENABLE_SENTIMENT="true"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o640)

    # -- stop ---------------------------------------------------------------------

    def _do_stop(self, record: dict[str, Any]) -> None:
        if record["legacy_pinned"]:
            raise RuntimeError("Refusing to stop a legacy pinned station")
        if record["active_campaign_count"] > 0:
            # References came back before the stop ran. Restore a truthful
            # state: only stations whose units actually run may claim active.
            if record["actual_state"] in ("active", "activating", "degraded", "stopping"):
                self._store.set_station_state(
                    record["id"], actual_state="active", desired_state="active"
                )
            else:
                self._store.set_station_state(
                    record["id"], actual_state="pending_capacity", desired_state="active"
                )
            log.info("stop cancelled: station %s regained references", record["local_station_id"])
            return
        station_id = record["local_station_id"]
        if not _STATION_ID_RE.match(station_id):
            raise RuntimeError(f"Refusing unsafe local station id: {station_id!r}")
        for template in _PIPELINE_UNITS:
            unit = template.format(sid=station_id)
            completed = self._run(["systemctl", "disable", "--now", unit], 90.0)
            if completed.returncode != 0:
                log.warning("systemctl disable --now %s: %s", unit, (completed.stderr or "").strip())
        self._store.set_station_state(
            record["id"], actual_state="stopped", desired_state="stopped", stop_after_utc=None
        )
        log.info(json.dumps({"event": "station_stopped", "station_id": station_id}))

    # -- background transitions ------------------------------------------------------

    def _schedule_due_stops(self) -> None:
        self._store.recompute_reference_counts(
            stop_grace_seconds=self._settings.RADIO_STATION_STOP_GRACE_SECONDS
        )
        for record in self._store.stations_due_for_stop():
            if record["actual_state"] in ("pending_capacity", "pending_probe"):
                # Nothing is running for these; a stop job has no units to touch.
                self._store.set_station_state(
                    record["id"],
                    desired_state="stopped",
                    actual_state="stopped",
                    stop_after_utc=None,
                )
                continue
            self._store.set_station_state(record["id"], desired_state="stopped")
            self._store.enqueue_job(record["id"], "stop")

    def _promote_pending_capacity(self) -> None:
        active = self._store.active_station_count()
        free = self._settings.RADIO_MAX_ACTIVE_STATIONS - active
        if free <= 0:
            return
        for record in self._store.list_managed_stations():
            if free <= 0:
                break
            if record["actual_state"] == "pending_capacity" and record["desired_state"] == "active":
                if int(record["active_campaign_count"] or 0) <= 0:
                    # No active campaign references it; the due-stop pass winds
                    # it down instead of burning a slot on an unused station.
                    continue
                self._store.set_station_state(record["id"], actual_state="pending_probe")
                self._store.enqueue_job(record["id"], "activate")
                free -= 1


def _env_escape(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="FireMud station reconciler")
    parser.add_argument("--once", action="store_true", help="process one cycle and exit")
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(settings.RADIO_DATABASE_PATH)
    database.connect()
    store = CatalogStore(database)
    store.migrate()
    client = RadioBrowserClient(
        user_agent=settings.RADIO_BROWSER_USER_AGENT,
        request_timeout_seconds=settings.RADIO_BROWSER_REQUEST_TIMEOUT_SECONDS,
        max_attempts=settings.RADIO_BROWSER_MAX_ATTEMPTS,
    )
    reconciler = Reconciler(settings, store, client)
    try:
        if args.once:
            reconciler.run_once()
        else:
            reconciler.run_forever()
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
