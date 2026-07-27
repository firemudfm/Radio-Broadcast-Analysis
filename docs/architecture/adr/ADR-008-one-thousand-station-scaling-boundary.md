# ADR-008 — The 1 000-station scaling boundary, stated honestly

Status: **Accepted** · Date: 2026-07-27

## Context

The requirement is to support 1 000+ stations. The target host is **4 ARM64
vCPUs and 8 GiB RAM**. Those two facts are not in conflict, but only if
"support 1 000 stations" is decomposed into things that are true.

## Decision

### 1. The claim, split into parts

| Claim | Status |
|---|---|
| Catalogue holds 1 000+ station records | **True today.** The bundled overlay plus Radio Browser already exceeds this. |
| Control plane holds 1 000+ unique station subscriptions | **True.** Rows in SQLite; proven by `tests/load/`. |
| Many campaigns map to shared stations without duplicating work | **True.** The whole point of ADR-003/ADR-010. |
| 10 000+ keywords and aliases index and match at speed | **True.** Proven by `tests/load/`. |
| One 4-vCPU / 8 GiB host continuously transcribes 1 000 live streams | **FALSE. This is not claimed anywhere.** |

> **Explicit non-claim.** This repository does not claim that one `c7g.xlarge`
> continuously transcribes 1 000 unique live stations. Anyone who reads such a
> claim into a benchmark number has misread it. The synthetic load test proves
> the *control plane and matcher* scale; it deliberately does not connect to
> audio.

### 2. Capacity is measured in unique active stations

`RADIO_MAX_ACTIVE_UNIQUE_STATIONS` — never campaign count, never keyword count,
never campaign-station row count. Default **8**, validated `1..512`.

The 8 comes from the memory budget in
[`../../research/TECHNOLOGY_RESEARCH.md §2`](../../research/TECHNOLOGY_RESEARCH.md)
and the FIFO per-group serialisation arithmetic in ADR-003 — not from a
measurement on the target host, which has not been run. It is a conservative
starting point to be raised by evidence.

### 3. Distinct counters, never conflated

`catalog_station_count`, `campaign_station_reference_count`,
`unique_requested_station_count`, `unique_active_station_count`,
`pending_capacity_station_count`, `reused_station_stream_count`, `worker_count`,
`transcription_queue_age_seconds`, `spool_usage_percent`. All exposed on
`GET /api/v1/monitoring/pipeline-capacity`.

`reused_station_stream_count` is the de-duplication proof: stations whose active
reference count exceeds 1, i.e. streams opened once and reused.

### 4. Overflow is a first-class state, not an error

Stations beyond capacity sit in `pending_capacity` with a reason string. They
are visible in the API, promoted automatically when a slot frees, and never
silently dropped. This mirrors the existing v0.4 behaviour, which already works
and is tested.

### 5. The sharding path

Deterministic assignment, computed identically in every process:

```python
shard = int.from_bytes(hashlib.blake2b(station_id.encode("utf-8"), digest_size=8).digest(), "big") % shard_count
```

**`blake2b`, not Python's built-in `hash()`.** `hash()` is randomised per process
by `PYTHONHASHSEED`, so two containers would disagree about which station they
own — a silent split-brain that would either double-connect or drop stations.
A test asserts a fixed known-vector mapping so the function cannot drift.

Today: `RADIO_LISTENER_SHARD_COUNT=1`, `RADIO_LISTENER_SHARD_INDEX=0`.
Tomorrow: run N listener containers with indices `0..N-1`, on one host or many.
No API change, no schema change, no campaign change — the planner already writes
`shard_index` on every subscription.

### 6. What must change beyond ~50 stations

Stated now so it is a plan, not a surprise:

| Stations | Change required |
|---|---|
| ≤ 8 | Default configuration on the current host. |
| 8–30 | Larger instance (more vCPU/RAM); raise `RADIO_MAX_ACTIVE_UNIQUE_STATIONS` and `RADIO_LISTENER_MAX_SESSIONS` after benchmarking. |
| 30–100 | Multiple listener shards + multiple transcription workers, still one control plane. Segment store must become `s3` if workers move off the listener's host. |
| 100–1 000 | **SQLite must be replaced with PostgreSQL.** Beyond roughly this point, single-writer serialisation on one file is the binding constraint, and the honest answer is a real database server, not a cleverer SQLite. Every table in the migration set is already written in portable SQL for that reason. |
| 1 000+ | Above, plus per-region control planes, and the S3 result layout already partitions by date. |

## Alternatives considered

1. **Claim 1 000 concurrent streams on the current host.** Rejected — false, and
   would produce a system that silently drops audio in production.
2. **Auto-scale listeners on queue depth.** Deferred: right idea, but it needs
   the shard model to exist first. That is what this ADR builds.
3. **One container per station.** Rejected explicitly by the brief and by
   arithmetic: 1 000 containers on 4 vCPUs is not a design.
4. **Cap campaigns instead of stations.** Rejected: campaigns are free; streams
   are not. Capping the wrong noun would block a customer with 200 campaigns on
   3 shared stations.
5. **Adopt PostgreSQL now.** Rejected for this iteration (see ADR-004) but named
   above with the concrete trigger, so it is a scheduled decision rather than an
   avoided one.

## Consequences

* Capacity is honest, visible and configurable.
* Sharding needs no redesign — only more containers and a count change.
* The load test proves control-plane scale and is explicitly labelled as not an
  audio-capacity claim.
* The PostgreSQL migration is pre-planned rather than emergency work.

## Operational risks

| Risk | Mitigation |
|---|---|
| Operator raises the limit past what the host can serve | `/healthz` reports `degraded` on sustained queue age; `docs/OPERATIONS.md` gives a staged ramp procedure with the metrics to watch |
| Shard count changed without restarting every listener | Each listener logs its `(count, index)` at startup and writes it to `worker_heartbeats`; the planner warns when heartbeating shards do not cover `0..count-1` |
| Two listeners with the same index | Detected via `worker_heartbeats` and surfaced in `/healthz` as a duplicate-shard warning |
| SQLite write contention creeps up unnoticed | `database_busy_retries` metric is the leading indicator and the documented PostgreSQL trigger |

## Security impact

Neutral. Sharding moves no trust boundary — every shard runs the same code with
the same SSRF, path and token controls. Multi-host distributed mode does require
the S3 segment store, which is why ADR-002 puts checksum verification on the read
path.

## Cost impact

The lever is instance count/size, and it is now explicit. 8 stations on one
`c7g.xlarge` is the unit of capacity; 100 stations is roughly 12 such units, or
fewer larger instances. That arithmetic being visible is the point of this ADR.

## Test requirements

* 1 000 synthetic stations + 200 campaigns + 10 000 keywords: planner produces
  exactly the DISTINCT station count, with timing assertions.
* Stations beyond the limit land in `pending_capacity`, never dropped.
* Promotion fills freed slots deterministically.
* `stable_shard_index` is stable across processes and matches fixed known
  vectors (regression against ever using `hash()`).
* Shard 0 of 1 owns every station; shards partition without overlap or gap for
  N = 2, 3, 4, 8.
* Every capacity counter is distinct and correct in a scenario where they would
  differ (many campaigns, few stations).
* `reused_station_stream_count` correctly reports shared streams.

## Reversal strategy

Set `RADIO_LISTENER_SHARD_COUNT=1` to collapse to a single listener; the hash
function maps everything to shard 0. Lower
`RADIO_MAX_ACTIVE_UNIQUE_STATIONS` to shed load — excess stations move to
`pending_capacity` and are wound down gracefully after the grace period, never
killed mid-conversation.
