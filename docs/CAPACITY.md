# Capacity: four numbers, and which ones are measured

The single easiest way to misread this system is to treat one of these as
another. Only two of the four have been proven.

| # | Number | Value | Proven by |
|---|---|---|---|
| 1 | Catalogue station count | 1,000+ | `pytest -m load` |
| 2 | **Requested** unique stations | 1,000 | `pytest -m load` |
| 3 | **Active** unique stations | **1** | configuration default; **not benchmarked above 1** |
| 4 | Real-time ASR capacity | **unknown** | **nothing. No benchmark has been run.** |

## What each one means

**1. Catalogue.** Rows in the station catalogue that can be browsed, searched
and selected. Costs a database row and an index entry. Cheap.

**2. Requested.** Distinct stations that campaigns have asked for. Control
plane: subscription rows, campaign-station mappings and one combined keyword
index per station. The load suite proves 1,000 distinct stations, 2,000
campaign-station references and 10,000 keyword bindings plan correctly and
stay bounded. Still cheap — nothing is decoding.

**3. Active.** Stations being decoded *right now*. Each one costs:

* one ffmpeg process holding a network stream open;
* one bounded RAM ring buffer (`RADIO_RING_BUFFER_SECONDS × 16 kHz × 2 bytes`,
  about 1.9 MiB at the default 60 s);
* a continuous share of one shared faster-whisper worker;
* spool writes for every retained segment.

On a 4 vCPU / 8 GiB aarch64 host that is *also* running the LLM and the API,
this is the number that runs out first. **The default is 1.**

**4. Real-time ASR capacity.** How many concurrent streams this host can
transcribe faster than they arrive. If ASR falls behind real time, the spool
grows without bound and audio is eventually dropped — silently, unless someone
is watching queue age. This number has never been measured.

## Requested is not active

Requesting a thousand stations is accepted. Exactly one is decoded. The other
999 are recorded as `pending_capacity` with a reason, and admitted as slots
free.

```
requested = 1000
active    = 1
pending_capacity = 999
```

`tests/load/test_thousand_stations.py` asserts exactly this, including that
active + pending accounts for every requested station — nothing silently
dropped, nothing silently started.

This is a queue, not a refusal. Rejecting the 999 would throw away a demand
signal that is worth keeping.

## What is NOT claimed

* **1,000 simultaneous live streams are not supported on this host.** Nothing in
  this repository should be read as claiming otherwise.
* The active limit of 1 is not a measured optimum. It is a conservative starting
  point chosen because no measurement exists.
* Raising the limit is not a configuration decision. It is a decision that
  requires the benchmark below.

## The benchmark harness

`scripts/benchmark-capacity.sh` runs a bounded live-capture benchmark at
1, 2, 5 and 8 distinct active stations. **It is not run as part of CI, and it
has not been run on the production host.** It needs real streams, real models
and a host that is not serving traffic.

For each step it records:

| Metric | Why it decides the limit |
|---|---|
| Connection success rate | Stations that will not stay connected are not capacity |
| Reconnect count | Churn costs CPU and loses audio |
| FFmpeg CPU | Decode cost per station, the linear term |
| Worker CPU | ASR cost, the term that saturates |
| **ASR real-time factor** | **> 1.0 means falling behind permanently** |
| Queue oldest-message age | The first visible symptom of falling behind |
| Queue backlog | Depth of the deficit |
| Dropped segments | Audio already lost |
| Ring-buffer overrun | The listener could not keep up with the stream |
| Memory / swap / OOM kills | Swap on this workload is the end of real-time |
| Spool growth rate | Time until the disk watermark trips |
| SQLite busy retries | Write contention between workers |
| Qwen latency | Analysis lag behind detection |

### Stop conditions

Raise the active limit only if **every** one holds at the target count, for a
sustained run, on the production host:

1. ASR real-time factor stays below 0.8 with headroom for bursts.
2. Queue oldest-message age is stable, not trending upward.
3. Zero dropped segments and zero ring-buffer overruns.
4. Spool usage stays below the warning watermark.
5. No swap use and no OOM kills.
6. Memory headroom remains for the LLM to answer without eviction.

If any of these fails at N stations, the supported limit is N−1.

## Raising the limit

```bash
# 1. Benchmark on the host, with nothing else running.
sudo scripts/benchmark-capacity.sh --stations 2 --minutes 30

# 2. Only if every stop condition holds, edit
#    /etc/radio-broadcast-analysis/application.env:
#      RADIO_MAX_ACTIVE_UNIQUE_STATIONS=2
#      RADIO_LISTENER_MAX_SESSIONS=2
#
# 3. Redeploy. The validator enforces sessions <= active <= requested.
```

Beyond what one host sustains, the path is horizontal: add listener hosts with
distinct `RADIO_LISTENER_SHARD_INDEX` values. Station-to-shard assignment is
deterministic and stable across restart, so no station is decoded twice.
