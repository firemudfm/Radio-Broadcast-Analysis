# v0.4.0 validation

## Automated (run on the packaging machine before shipping)

- `python -m pytest tests/ -q` -> 131 passed (Python 3.12, isolated venv).
- `python -m compileall app tools` -> clean.
- Curated overlay built from `radio-database-main.zip`: 419 overrides,
  354 deletions, including Hertz 87.9
  (`0e30b79d-3977-4bb0-9e83-a1914cd757d0`) and MANGORADIO
  (`78012206-1aa1-11e9-a80b-52543be04c81`). Deletion markers parsed in both
  boolean and legacy string "true" forms.

Coverage: overlay build (zip-slip, duplicate UUID, malformed URL, deletion
parsing), mirror discovery/failover/retry/coercion/caching, SSRF (loopback,
RFC1918, link-local metadata, IPv6 loopback/ULA/v4-mapped, decimal/hex IPs,
mixed public+private DNS answers), catalogue merge + deletion filtering, no
stream URL in any public payload, pagination limits, capacity admission
(2-station hard limit), estimates for explicit/country_top/country_all,
per-campaign station cap, shared reference counting with stop grace and
resume, legacy pinned protection, reconciler activate/stop/probe fail-closed
paths with mocked systemd/ffprobe, station env generation from the installed
hertz879 template, campaign back-compat (`station_ids`) plus the new
`station_selection` contract, and the v0.3.1 -> v0.4.0 SQLite migration
against a real old-schema database.

## On EC2 after deployment

1. `sudo ./deploy/upgrade-to-v0.4.0-amazon-linux.sh` ends with SUCCESS.
2. `./deploy/audit-v040.sh` -> all PASS.
3. `curl -s localhost:8788/healthz` -> `"version":"0.4.0"`.
4. `curl -s 'localhost:8788/api/v1/radio-catalog/stations?country_code=DE&query=hertz'`
   returns the curated Hertz 87.9 record with `monitoring_status`.
5. `curl -s localhost:8788/api/v1/monitoring/capacity` shows
   `active_station_limit: 2` and hertz879 counted active.
6. Probe a station, watch `journalctl -u radio-station-reconciler -f` for
   `job_start` -> `station_active`, verify the three `rb-<uuid>` units.
7. Confirm the existing Supersuckers mention flow still works end to end
   (dashboard, transcript detail, audio token, audio bytes).

## Hotfix v0.4.1 (2026-07-18)

Field bug: every catalogue station probe failed on EC2 with
`Failed to set value '20' for option 't': Option not found`. The reconciler
passed `-t <seconds>` to ffprobe, but `-t` is an ffmpeg-only option; real
ffprobe (5.1.9 on Amazon Linux 2023) rejects it and exits non-zero, so all
`activate` jobs failed and stations stayed in `failed_probe`. Local tests
missed it because the subprocess runner is mocked.

Fix: drop `-t` from the probe command; read time is already bounded by
`-analyzeduration 10M` plus the subprocess timeout. Regression test added in
`tests/test_reconciler.py` (asserts `-t` never appears in the ffprobe
command). Deployed to EC2 as a single-file update of
`app/station_reconciler.py` followed by `systemctl restart
radio-station-reconciler`.

## v0.4.1 addition (2026-07-18): configurable mention window

`RADIO_MENTION_WINDOW_DAYS` (default 7, validated 1..31) now drives the
dashboard sentiment summary and the per-campaign recent-mention counts.
The dashboard payload exposes `mention_window_days` so clients can label
the window. The per-campaign count SQL switched from
`datetime('now','-7 days')` (mismatched separator vs stored ISO strings)
to a Python-computed ISO cutoff, consistent with sentiment_summary.
Pilot EC2 env sets the window to 1 (last 24 hours). Regression test:
`test_mention_window_days_bounds_summary_and_campaign_counts` (133 total).
Also removed a duplicate `model_validator` import in app/models.py.

## v0.4.1 fix (2026-07-18): unreferenced stations wound down reliably

Field bug: after its campaign was deleted, a promoted station kept
recording for hours with active_campaign_count=0. Chain: promotion ignored
references; activation clears stop_after_utc; recompute only re-armed the
timer on a refs 1 -> 0 transition; stations_due_for_stop skipped
pending_capacity/pending_probe states, leaving expired timers stuck.

Fixes: (1) promotion skips zero-reference stations; (2) recompute re-arms
the stop timer for any unreferenced station with desired_state=active;
(3) due pending_capacity/pending_probe stations are marked stopped
directly, no systemd job; (4) a queued activate job is refused when
desired_state is no longer active. Regression tests: 4 new, 137 total.
Legacy pinned stations remain exempt from every wind-down path.

### Review-driven repairs to the wind-down fix (same day)

Adversarial review confirmed the first version of the wind-down fix broke
two flows, both repaired:

1. Pause longer than the grace period at full capacity left members
   stopped forever after resume. recompute_reference_counts now revives
   referenced stations that are actual_state=stopped (desired=active,
   actual=pending_capacity) so the promotion pass restarts them.
2. on_campaign_status_change enqueued real stop jobs for never-started
   pending_capacity/pending_probe stations; it now direct-marks them
   stopped like the reconciler does, and _do_stop's refs-regained cancel
   branch restores pending_capacity (not a phantom active) when no units
   ever ran.

Policy made explicit instead of silent: a campaign-less manual activation
(POST /monitoring/stations/{uuid}/activate) runs only for the grace
period; the response detail now says so. Regression tests: 7 new since
v0.4.0 in this area, suite at 140.

## v0.4.1 additions (2026-07-18, later): full-clip playback, capacity 6, hertz879 retired

- RADIO_MENTION_AUDIO_PAD_SECONDS (default 2.0, validated 0..900): playback
  padding around the keyword inside the clean-speech clip. Pilot env sets
  900, so every mention plays the whole captured discussion segment.
  Computed at read time, so existing mentions serve full clips immediately.
  Database._mention became an instance method for this. Test:
  test_mention_audio_pad_expands_playback_to_full_clip (suite 141).
- Pilot env: RADIO_MAX_ACTIVE_STATIONS 2 -> 6, RADIO_LEGACY_PINNED_STATION_IDS
  emptied. hertz879 units disabled manually (the stop path's rb-* id guard
  is intentional), DB row set legacy_pinned=0/stopped. The station env
  template file /etc/radio-pipeline/stations/hertz879.env stays: it is the
  source of truth for generated rb-* station env files.
- Caveat: resuming an old campaign that references hertz879 will revive it
  to pending_capacity and the probe will fail (legacy stations cannot be
  probed); delete those old test campaigns instead.
