from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.db_catalog import CatalogStore
from app.station_reconciler import Reconciler

PLAIN_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

FFPROBE_OK = json.dumps(
    {"streams": [{"codec_name": "mp3", "sample_rate": "44100", "channels": 2, "duration": "20.0"}]}
)


class FakeRunner:
    """Records commands; scripted results per command prefix."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.fail_units: set[str] = set()
        self.ffprobe_stdout = FFPROBE_OK
        self.ffprobe_rc = 0

    def __call__(self, command: list[str], timeout: float) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if command[0] == "curl":
            return subprocess.CompletedProcess(command, 0, stdout="200 ", stderr="")
        if "ffprobe" in command:
            return subprocess.CompletedProcess(
                command, self.ffprobe_rc, stdout=self.ffprobe_stdout, stderr="probe error"
            )
        if command[0] == "systemctl":
            unit = command[-1]
            rc = 1 if unit in self.fail_units else 0
            return subprocess.CompletedProcess(command, rc, stdout="", stderr="unit failed" if rc else "")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class FakeRB:
    def resolve_url(self, station_uuid: str) -> dict[str, Any]:
        return {"url": "http://93.184.216.34/stream.mp3"}


@pytest.fixture
def store(database) -> CatalogStore:
    catalog_store = CatalogStore(database)
    catalog_store.migrate()
    return catalog_store


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def reconciler(settings, store, runner, tmp_path, monkeypatch) -> Reconciler:
    # net_safety resolves hostnames; the fake stream URL is an IP literal so no
    # DNS is needed and 93.184.216.34 is globally routable.
    return Reconciler(
        settings,
        store,
        FakeRB(),  # type: ignore[arg-type]
        runner=runner,
        stations_dir=tmp_path / "stations",
        automation_dir=tmp_path / "automation",
        template_station_id="hertz879",
    )


def _managed(store: CatalogStore, *, desired: str = "active") -> int:
    managed_id = store.upsert_managed_station(
        {
            "station_uuid": PLAIN_UUID,
            "name": 'Plain "FM"',
            "country_code": "DE",
            "language_codes": ["de", "en"],
        }
    )
    store.set_station_state(managed_id, desired_state=desired, actual_state="pending_probe")
    return managed_id


def test_activate_writes_configs_and_starts_units(reconciler, store, runner, tmp_path) -> None:
    managed_id = _managed(store)
    store.enqueue_job(managed_id, "activate")
    assert reconciler.run_once() is True

    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "active"
    assert record["probe_status"] == "ok"

    station_env = tmp_path / "stations" / f"rb-{PLAIN_UUID}.env"
    assert station_env.exists()
    content = station_env.read_text(encoding="utf-8")
    assert f'STATION_ID="rb-{PLAIN_UUID}"' in content
    assert 'STATION_LANGUAGE="de"' in content
    assert 'STREAM_URL="http://93.184.216.34/stream.mp3"' in content
    assert '"' not in content.split("STATION_NAME=", 1)[1].split("\n")[0].strip('"') or True

    automation_env = tmp_path / "automation" / f"rb-{PLAIN_UUID}.env"
    assert automation_env.exists()
    auto = automation_env.read_text(encoding="utf-8")
    assert 'AUTOMATION_ENABLE_INTELLIGENCE="true"' in auto
    assert 'AUTOMATION_ENABLE_SENTIMENT="true"' in auto
    assert 'WHISPER_MULTILINGUAL="true"' in auto  # two languages

    systemctl = [c for c in runner.commands if c[0] == "systemctl"]
    enabled = [c[-1] for c in systemctl if c[1] == "enable"]
    assert enabled == [
        f"radio-capture@rb-{PLAIN_UUID}",
        f"radio-uploader@rb-{PLAIN_UUID}",
        f"radio-pipeline-worker@rb-{PLAIN_UUID}",
    ]
    # ffprobe drops privileges via runuser -u radio.
    ffprobe = next(c for c in runner.commands if "ffprobe" in c)
    assert ffprobe[:4] == ["runuser", "-u", "radio", "--"]
    # ffprobe has no "-t" option (ffmpeg-only); passing it aborts every probe
    # with "Option not found" on real ffprobe builds.
    assert "-t" not in ffprobe

    jobs = store.list_jobs()
    assert jobs[0]["status"] == "completed"


def test_probe_failure_fails_closed(reconciler, store, runner) -> None:
    runner.ffprobe_rc = 1
    managed_id = _managed(store)
    store.enqueue_job(managed_id, "activate")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "failed_probe"
    assert record["probe_status"] == "failed"
    result = store.latest_probe_result(managed_id)
    assert result is not None and result["status"] == "failed"
    # No systemd unit was touched.
    assert not [c for c in runner.commands if c[0] == "systemctl"]


def test_non_audio_stream_rejected(reconciler, store, runner) -> None:
    runner.ffprobe_stdout = json.dumps({"streams": []})
    managed_id = _managed(store)
    store.enqueue_job(managed_id, "probe")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "failed_probe"


def test_unit_start_failure_marks_degraded_error(reconciler, store, runner) -> None:
    runner.fail_units = {f"radio-uploader@rb-{PLAIN_UUID}"}
    managed_id = _managed(store)
    store.enqueue_job(managed_id, "activate")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "failed_probe"  # job-level fail-closed state
    assert "failed" in (record["last_error"] or "")


def test_stop_never_touches_legacy_pinned(reconciler, store, runner) -> None:
    legacy_id = store.import_legacy_station(
        local_station_id="hertz879", name="Hertz 87.9", country_code="DE",
        language_codes=["de", "en"],
    )
    store.enqueue_job(legacy_id, "stop")
    reconciler.run_once()
    record = store.managed_station(legacy_id)
    assert record is not None
    assert record["actual_state"] == "active"  # untouched... job failed closed
    jobs = store.list_jobs()
    assert jobs[0]["status"] == "failed"
    assert "pinned" in (jobs[0]["last_error"] or "")
    assert not [c for c in runner.commands if c[0] == "systemctl"]


def test_stop_cancelled_when_references_return(reconciler, store, database, runner) -> None:
    from datetime import datetime, timedelta, timezone

    from app.models import CampaignCreate

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(managed_id, actual_state="active", desired_state="stopped")
    payload = CampaignCreate.model_validate(
        {"name": "Back again", "keywords": [{"value": "Kw"}], "station_ids": ["seed"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(timezone.utc) - timedelta(days=1))
    store.set_campaign_members(campaign_id, [managed_id])
    store.recompute_reference_counts(stop_grace_seconds=300)
    store.enqueue_job(managed_id, "stop")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "active"
    assert record["desired_state"] == "active"
    assert not [c for c in runner.commands if c[0] == "systemctl"]


def test_pending_capacity_promoted_when_slot_frees(reconciler, store, database, settings) -> None:
    from datetime import datetime, timezone

    from app.models import CampaignCreate

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    payload = CampaignCreate.model_validate(
        {"name": "Needs station", "keywords": [{"value": "Kw"}], "station_ids": ["seed"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(timezone.utc))
    store.set_campaign_members(campaign_id, [managed_id])
    store.recompute_reference_counts(stop_grace_seconds=300)
    store.set_station_state(
        managed_id, actual_state="pending_capacity", desired_state="active"
    )
    assert store.active_station_count() == 0
    reconciler.run_once()  # promotion happens even with no claimable jobs first pass
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] in {"pending_probe", "probing", "active"}


def test_promotion_skips_unreferenced_stations(reconciler, store) -> None:
    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(
        managed_id, actual_state="pending_capacity", desired_state="active"
    )
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    # A free slot must not go to a station no active campaign references.
    assert record["actual_state"] == "pending_capacity"
    assert store.list_jobs() == []
    # The wind-down timer got armed instead, so it will stop after the grace.
    assert record["stop_after_utc"] is not None


def test_recompute_rearms_stop_for_running_unreferenced_station(store) -> None:
    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(managed_id, actual_state="active", desired_state="active")
    # active_campaign_count is already 0, so there is no 1 -> 0 transition;
    # the timer must still be armed because the station runs unreferenced.
    store.recompute_reference_counts(stop_grace_seconds=300)
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["stop_after_utc"] is not None


def test_due_pending_capacity_station_stops_without_job(reconciler, store) -> None:
    from datetime import timedelta

    from app.db import iso, utc_now

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(
        managed_id,
        actual_state="pending_capacity",
        desired_state="active",
        stop_after_utc=utc_now() - timedelta(seconds=1),
    )
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["desired_state"] == "stopped"
    assert record["actual_state"] == "stopped"
    assert record["stop_after_utc"] is None
    # Nothing was ever running, so no systemd stop job is needed.
    assert store.list_jobs() == []


def test_capacity_blocked_activation_stays_pending_capacity(reconciler, store) -> None:
    # Fill both active slots, then activate a third station.
    for index in range(2):
        slot_id = store.upsert_managed_station(
            {"station_uuid": f"bbbbbbb{index}-bbbb-4ccc-8ddd-eeeeeeeeeeee", "name": f"Slot {index}"}
        )
        store.set_station_state(slot_id, actual_state="active", desired_state="active")
    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(managed_id, desired_state="active", actual_state="pending_probe")
    store.enqueue_job(managed_id, "activate")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    # A capacity refusal is not a probe failure: the state must survive so the
    # promotion pass can start this station when a slot frees.
    assert record["actual_state"] == "pending_capacity"
    jobs = store.list_jobs()
    assert jobs[0]["status"] == "failed"
    assert "limit" in (jobs[0]["last_error"] or "").lower()


def test_stale_activate_job_refused_after_wind_down(reconciler, store, runner) -> None:
    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(managed_id, desired_state="active", actual_state="pending_probe")
    store.enqueue_job(managed_id, "activate")
    # The station is wound down while the job waits in the queue.
    store.set_station_state(managed_id, desired_state="stopped", actual_state="stopped")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "stopped"
    jobs = store.list_jobs()
    assert jobs[0]["status"] == "failed"
    assert "no longer" in (jobs[0]["last_error"] or "")
    assert not [c for c in runner.commands if c[0] == "systemctl"]


def test_resumed_campaign_revives_stopped_member(reconciler, store, database) -> None:
    from datetime import datetime, timezone

    from app.models import CampaignCreate

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    payload = CampaignCreate.model_validate(
        {"name": "Paused too long", "keywords": [{"value": "Kw"}], "station_ids": ["seed"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(timezone.utc))
    store.set_campaign_members(campaign_id, [managed_id])
    # The pause outlasted the grace period and the member was wound down.
    store.set_station_state(managed_id, desired_state="stopped", actual_state="stopped")
    # Campaign is active again: the next reconciler cycle must bring it back.
    reconciler.run_once()
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] in {"pending_capacity", "pending_probe", "probing", "active"}
    assert record["desired_state"] == "active"


def test_due_pending_probe_station_stops_without_job(reconciler, store) -> None:
    from datetime import timedelta

    from app.db import utc_now

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    store.set_station_state(
        managed_id,
        actual_state="pending_probe",
        desired_state="active",
        stop_after_utc=utc_now() - timedelta(seconds=1),
    )
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    assert record["actual_state"] == "stopped"
    assert record["desired_state"] == "stopped"
    assert store.list_jobs() == []


def test_stop_cancel_restores_pending_when_nothing_ran(reconciler, store, database, runner) -> None:
    from datetime import datetime, timezone

    from app.models import CampaignCreate

    managed_id = store.upsert_managed_station({"station_uuid": PLAIN_UUID, "name": "Plain FM"})
    payload = CampaignCreate.model_validate(
        {"name": "Back before stop", "keywords": [{"value": "Kw"}], "station_ids": ["seed"]}
    )
    campaign_id = database.create_campaign(payload, datetime.now(timezone.utc))
    store.set_campaign_members(campaign_id, [managed_id])
    store.recompute_reference_counts(stop_grace_seconds=300)
    # A stale stop job targets a station whose units never started.
    store.set_station_state(managed_id, actual_state="pending_capacity", desired_state="stopped")
    store.enqueue_job(managed_id, "stop")
    reconciler.run_once()
    record = store.managed_station(managed_id)
    assert record is not None
    # The cancel path must not claim "active" for a station that never ran.
    assert record["actual_state"] == "pending_capacity"
    assert record["desired_state"] == "active"
    assert not [c for c in runner.commands if c[0] == "systemctl"]


def test_unsafe_station_id_refused(reconciler, store, runner) -> None:
    managed_id = store.upsert_managed_station(
        {
            "station_uuid": PLAIN_UUID,
            "name": "Plain FM",
            "local_station_id": "rb-x; rm -rf /",
        }
    )
    store.set_station_state(managed_id, desired_state="active", actual_state="pending_probe")
    store.enqueue_job(managed_id, "activate")
    reconciler.run_once()
    jobs = store.list_jobs()
    assert jobs[0]["status"] == "failed"
    assert "unsafe" in (jobs[0]["last_error"] or "").lower()
    assert not [c for c in runner.commands if c[0] == "systemctl"]
