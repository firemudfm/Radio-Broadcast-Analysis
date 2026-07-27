# ADR-001 — Legacy and shared-SQS pipeline modes

Status: **Accepted** · Date: 2026-07-27 · Supersedes: none

## Context

The production EC2 instance runs the architecture described in
[`../CURRENT_ARCHITECTURE.md`](../CURRENT_ARCHITECTURE.md): per-station systemd
capture/uploader/pipeline units driven by a root reconciler, S3 polling for
results, and one mention row per (campaign, keyword) pair. It works, it is
deployed, and it is the subject of a 140-test regression suite.

The new shared-station SQS pipeline changes the process topology, the storage
layout, the mention data model and the deployment mechanism. Shipping it as a
replacement would mean the next `git pull` on the running instance breaks a live
pilot.

## Decision

Introduce `RADIO_PIPELINE_MODE`, an enumerated setting with exactly two accepted
values, **defaulting to `legacy`**:

| Value | Behaviour |
|---|---|
| `legacy` | Byte-identical to `d82d847`. No new component constructs, no new table is required at runtime, no new environment variable is mandatory. |
| `shared_sqs` | The new planner / listener / worker topology is wired; legacy sync and reconciler paths remain available but are not started by the new Compose stack. |

Any other value raises a `ValueError` during `Settings` validation, so the
process fails fast at startup rather than running in an ambiguous half-state.

Mode selection happens in exactly one place — the FastAPI lifespan and each
worker's `main()` — never scattered through call sites. Services that are
mode-independent (`net_safety`, `audio`, `preview`, `catalog`, `radio_browser`)
are constructed unconditionally.

New database tables are created by migrations that run in **both** modes,
because a migration that only runs in one mode makes the two modes' schemas
diverge and makes switching modes a data-migration event. Creating empty tables
is free; populating them is mode-gated.

## Alternatives considered

1. **Replace outright.** Rejected: breaks a running pilot on merge, and gives no
   way to A/B the two paths against the same stations.
2. **Separate branch / separate repository.** Rejected: guarantees drift, and
   the shared components (SSRF guard, audio tokens, catalogue, API models) would
   be duplicated and diverge.
3. **Per-feature flags** (`RADIO_USE_SQS`, `RADIO_USE_LISTENER`, …). Rejected:
   2ⁿ combinations, most of them untested and several incoherent (a listener
   producing segments with no consumer). One enum is one supported matrix.
4. **Per-station mode.** Deferred, not rejected. It is the natural migration
   path once `shared_sqs` is proven; the planner already keys everything on
   `station_id`, so a per-station column can be added without redesign.

## Consequences

* Two code paths must be maintained until the new one is proven on the new EC2.
* Every new module must be import-safe when the mode is `legacy` — no
  import-time SQS client construction, no import-time model loading.
* The test suite runs both modes; `tests/unit/test_pipeline_mode.py` asserts the
  legacy default and that unknown values are rejected.
* Documentation must state the mode for every operational instruction.

## Operational risks

| Risk | Mitigation |
|---|---|
| Operator sets `shared_sqs` without queues/spool configured | `Settings` validation requires both queue URLs and a writable spool root when the mode is `shared_sqs`; startup fails with a named error |
| Both pipelines run against one station and double-produce | The Compose stack never starts the legacy reconciler; `docs/OPERATIONS.md` documents the cutover as *stop legacy units first* |
| Silent mode drift between containers | Every service logs its resolved mode once at startup and reports it in `/healthz` |

## Security impact

Neutral-to-positive. No security control is mode-gated: the SSRF validator, the
audio-key allowlist and the HMAC token verification run identically in both
modes. Adding a mode does not add an authentication surface — `auth_mode` stays
`none` (§32 of the brief) and is unchanged.

## Cost impact

Zero in `legacy`. In `shared_sqs`, two SQS FIFO queues and their DLQs
(request/DLQ pairs) plus a small increase in S3 PUTs for final results. SQS FIFO
pricing is per-request; at 8 stations × 3 segments/minute the transcription queue
sees ≈ 1 400 send+receive+delete request-triples/hour.

## Test requirements

* `Settings(RADIO_PIPELINE_MODE="legacy")` is the default with no env set.
* Unknown values raise `ValidationError`.
* `shared_sqs` without `RADIO_TRANSCRIPTION_QUEUE_URL` raises.
* Existing 140 tests pass unmodified (regression gate).
* An app built in `legacy` mode exposes no new required request fields.

## Reversal strategy

Set `RADIO_PIPELINE_MODE=legacy` and restart. No data migration, no rollback
script, no schema change — the new tables simply stop receiving writes. Because
migrations are mode-independent, a database that ran `shared_sqs` is still a
valid `legacy` database.
