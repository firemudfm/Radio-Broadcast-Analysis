# ADR: shared-station SQS is the only production pipeline

**Status:** accepted
**Supersedes:** ADR-001 (legacy and shared pipeline modes)

## Context

The system carried two processing pipelines behind `RADIO_PIPELINE_MODE`:

* **legacy** — one systemd unit set per station (`radio-capture@`,
  `radio-uploader@`, `radio-pipeline-worker@`), audio written to S3 and polled
  back out, with `app/station_reconciler.py` as the only process permitted to
  touch systemd. This was the default.
* **shared_sqs** — one listener per *distinct* station, a bounded RAM ring
  buffer, a local EBS spool, SQS FIFO queues and shared ASR/LLM workers.

The switch was introduced so an existing deployment could upgrade inert: install
the new code, keep running the old path, flip the mode when ready. That was the
right call while the shared pipeline was unproven.

It stopped being the right call once the shared pipeline was the one being
deployed, for reasons that compounded:

* **Two runtimes, one of them untested in anger.** Every change had to be
  reasoned about twice. The legacy path had no container images, no health
  gates and no rollback story — it could not have been deployed by the current
  automation even if someone wanted to.
* **The default was the wrong one.** `RADIO_PIPELINE_MODE` defaulted to
  `legacy`, so a fresh host with no configuration ran the pipeline nobody
  intended to run.
* **Readiness was ambiguous.** `/readyz` returned ready on the database alone in
  legacy mode. A shared-pipeline host that lost every worker could not be
  distinguished from a healthy legacy one by looking at the field alone.
* **The economics only exist in one of them.** One decode shared by a hundred
  campaigns, one keyword index per station, one analysis per conversation —
  none of that is true of the per-station-unit design.

## Decision

**Shared-station + SQS is the only production processing pipeline.** The mode
switch, the systemd runtime and their configuration are removed.

Removed: `RADIO_PIPELINE_MODE`, `PipelineMode`, `shared_pipeline_enabled`,
`RADIO_MAX_ACTIVE_STATIONS`, `RADIO_RECONCILER_POLL_SECONDS`,
`app/station_reconciler.py`, the four systemd unit templates, the legacy
install/upgrade/audit deploy scripts, and the legacy-only readiness branch.

Kept, and explicitly *not* second pipelines:

* `RADIO_QUEUE_BACKEND=memory` and the fake ASR/LLM engines — deterministic test
  doubles. Production is barred from both by `APP_ENV=production`.
* The `local` segment store and the S3 segment-store adapter — storage backends
  for one pipeline, not alternative pipelines.
* The `api` / `core` / `full` deployment stages — **rollout** stages. `full`
  always runs the one architecture.

## A stale environment fails loudly

`Settings` uses `extra="ignore"`, so a leftover `RADIO_PIPELINE_MODE=legacy`
would be silently dropped and the host would run the shared pipeline anyway.
The operator would never learn their file was stale — until the day they changed
that value and nothing happened.

Removed settings are therefore rejected by name at start-up, with the
replacement stated. Refusing to start is the smaller failure: it happens once,
loudly, at deploy time, in front of the person who caused it.

## Why the deployment stages stay

They were nearly removed alongside the mode switch, and that would have been a
mistake. A stage is not a pipeline: `api` starts the control plane only, `core`
adds the planner, `full` adds capture, ASR and the LLM. They exist so a fresh
host is widened one step at a time, with health verified between steps, rather
than discovering at the last step that the box cannot run the workers. Removing
them would have made first install all-or-nothing.

## Why requested 1,000 does not mean active 1,000

The control plane scales to a thousand distinct stations: rows, campaign
mappings, keyword indexes, planning and matching are all proven there by
`pytest -m load`. Decoding does not scale the same way. Each active station
costs one ffmpeg decode, one ring buffer and a share of ASR on a 4 vCPU / 8 GiB
aarch64 host that is also running an LLM and the API.

So the two limits are separate settings with a thousandfold gap between their
defaults:

```
RADIO_MAX_REQUESTED_UNIQUE_STATIONS = 1000   # control plane
RADIO_MAX_ACTIVE_UNIQUE_STATIONS    = 1      # compute
```

Requesting more than the active limit is accepted and recorded; the excess is
parked as `pending_capacity`, with a reason, and admitted as slots free. That is
a queue, not a refusal, and it keeps a demand signal that a rejection would
throw away.

**No benchmark has been run.** Raising the active limit without one is how the
spool fills and audio is lost silently. `docs/CAPACITY.md` defines the harness.

## Data

Nothing is dropped. The two pipelines always shared one SQLite schema, so
removing one was never a data-migration event: campaigns, stations, keywords and
mentions written by the legacy path remain readable and are picked up by the
planner as ordinary rows. Historical tables are retained; retiring one is a
separate, separately approved exercise with its own backup.

## Scaling path

Horizontal, not vertical, and already wired:

1. Raise `RADIO_MAX_ACTIVE_UNIQUE_STATIONS` on the current host, guided by the
   benchmark, until ASR real-time factor or spool growth says stop.
2. Add listener hosts. Station-to-shard assignment is deterministic
   (`stable_shard_index`), stable across restart, and validated so no station
   belongs to two shards; heartbeats and lease expiry prevent duplicate
   ownership.
3. Add transcription workers. They are stateless SQS consumers; FIFO
   `MessageGroupId` is the station id, so per-station ordering survives.

## Rollback

Unchanged and artifact-only: releases are immutable, identified by **commit +
stage**, and a rollback restores an existing release and existing images with
`--no-build --pull never`. The database is never restored automatically — that
would discard every mention written since the backup.

Rolling back *this* change means deploying a commit from before it. The legacy
runtime is in git history; it is not in the deployed artifact.

## Consequences

* One runtime to reason about, test, deploy and operate.
* `/readyz` means one thing: every worker role is alive and the queues are
  configured. A host with no workers is not ready, and says so.
* A fresh host runs the intended pipeline with no configuration.
* Anyone still running the legacy pipeline cannot upgrade in place by deploying
  this commit — they get a start-up failure naming the setting to remove. That
  is deliberate, and it is the only migration step.
