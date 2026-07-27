# Operations

Day-to-day running of the shared-station pipeline: what to watch, what the
numbers mean, and what to do when they go wrong.

---

## 1. The numbers, and what each one actually means

`GET /api/v1/monitoring/pipeline`

These counters are kept deliberately separate. Conflating them is how a system
starts claiming capacity it does not have.

| Field | Meaning | Grows with |
|---|---|---|
| `catalog_station_count` | Stations known to the catalogue | Catalogue imports. **No load implication.** |
| `campaign_station_reference_count` | `(campaign, station)` rows | Campaigns. **No load implication.** |
| `unique_requested_station_count` | Distinct stations some active campaign wants | Distinct station selections |
| `unique_active_station_count` | Distinct stations actually being listened to | **This is the load number.** |
| `pending_capacity_station_count` | Wanted but over the limit | Demand beyond capacity |
| `reused_station_stream_count` | Stations shared by 2+ campaigns | Sharing — higher is better |
| `worker_count` | Workers with a live heartbeat | Deployment |
| `queue_age_seconds` | Age of the oldest unfinished work | Backlog |
| `spool_usage_percent` | Spool filesystem usage | Retention vs throughput |

Forty campaigns each selecting fifty stations produce a
`campaign_station_reference_count` of 2,000 and a
`unique_active_station_count` of 8. Only the second one costs CPU.

**Never quote reference count as capacity.** The measured control-plane load
test (`make test-load`) runs 1,000 stations, 2,000 references and 8 active —
that proves the control plane scales, and says nothing about how many live
streams one host can transcribe.

---

## 2. Daily checks

```bash
scripts/smoke-test.sh                       # end-to-end, read-only
docker compose ps                           # container states
docker stats --no-stream                    # memory against the limits
```

Healthy looks like: `/readyz` 200, four components `ok`, spool `ok`,
`queue_age_seconds` small or `null`, no growth in `outbox.failed`.

---

## 3. Alert thresholds

| Condition | Severity | Meaning | Action |
|---|---|---|---|
| `/readyz` 503 for > 5 min | **Page** | A required worker is down or the spool is full | §4 |
| `queue_age_seconds > 900` | **Page** | Consumers cannot keep up; audio ages out before processing | §5 |
| `spool_pressure = emergency` | **Page** | New segments are being refused | §6 |
| `spool_pressure = pause` | Warn | Admission stopped for new stations | §6 |
| `outbox.failed > 0` | Warn | Work was abandoned after max attempts | §7 |
| `pending_capacity_station_count > 0` | Info | Demand exceeds the configured limit | §8 |
| `components.listener = stale` | **Page** | No live audio is being captured | §4 |

`pending_capacity` is **informational**, not a fault. It is the system
correctly refusing to oversubscribe, and it is visible precisely so it is never
a silent drop.

---

## 4. A worker is down or stale

```bash
docker compose ps
docker compose logs --tail=200 listener
```

Workers write a heartbeat row; `stale` means the process exists but stopped
beating (usually wedged in a network call), while `absent` means it never ran.

```bash
docker compose restart listener
```

A `degraded` heartbeat is **not** a reason to restart. It means the worker is
alive and reporting a real condition — spool pressure, say — and restarting it
would drop whatever it was holding without fixing the cause.

---

## 5. The queue is backing up

Established first: which stage?

```bash
sqlite3 /var/lib/radio/database/radio.db \
  "SELECT status, count(*) FROM transcription_jobs GROUP BY status;
   SELECT status, count(*) FROM analysis_jobs GROUP BY status;
   SELECT status, count(*) FROM outbox_events GROUP BY status;"
```

* **Transcription pending grows** → ASR is the bottleneck. Add a
  `transcription-worker` replica, or reduce `RADIO_MAX_ACTIVE_UNIQUE_STATIONS`.
  Raising `RADIO_ASR_CPU_THREADS` past 2 usually makes things *worse* on 4
  vCPUs: it starves the listener, and dropped live audio cannot be recovered
  while a slow queue can.
* **Analysis pending grows** → the LLM is the bottleneck. It is the one stage
  that can safely wait. Check the circuit breaker in the analysis worker's
  logs; sustained fallbacks mean `llama-server` is unhealthy.
* **Outbox pending grows** → SQS is unreachable or the planner is down. The
  outbox is doing its job: nothing is lost, it is queued.

---

## 6. Spool pressure

Watermarks escalate rather than switch:

| Level | Default | Behaviour |
|---|---|---|
| warning | 70% | Logged; health degrades |
| pause | 85% | New segments refused; existing stations keep running |
| emergency | 90% | Expired data swept aggressively; `/readyz` fails |

```bash
df -h /var/lib/radio/spool
sqlite3 /var/lib/radio/database/radio.db \
  "SELECT disposition, count(*), sum(size_bytes)/1048576 AS mib
   FROM audio_segments GROUP BY disposition;"
```

* Large `disposable` → cleanup is behind. Check its logs; it deletes in bounded
  batches by design so it cannot monopolise the write lock.
* Large `pending` → transcription is behind (§5). **The cleanup worker will
  never delete these**, at any watermark, because they have not been
  transcribed yet — deleting them would destroy mentions that were about to be
  found. Fix the throughput, do not force a delete.
* Large `retained` → these belong to mentions. Reduce
  `RADIO_EVIDENCE_RETENTION_DAYS` if the spool is genuinely too small.

Never `rm -rf` the spool while workers run. Files are referenced by
`audio_segments` rows, and a job pointing at a deleted file becomes a
`segment_missing` permanent failure — which loses the mention.

---

## 7. Failed outbox events

```bash
sqlite3 /var/lib/radio/database/radio.db \
  "SELECT queue_name, count(*), max(last_error)
   FROM outbox_events WHERE status='failed' GROUP BY queue_name;"
```

`failed` rows are **never auto-pruned**: they are the evidence that work was
lost, and an operator needs to see them. After fixing the cause, return them to
the dispatcher:

```bash
sqlite3 /var/lib/radio/database/radio.db \
  "UPDATE outbox_events SET status='pending', attempts=0,
   available_at_utc=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE status='failed';"
```

Safe to run: consumer inboxes make a resend a no-op.

---

## 8. Capacity changes

`RADIO_MAX_ACTIVE_UNIQUE_STATIONS` is the only knob that changes real load.

The default of 8 is a **conservative starting point for 4 vCPU / 8 GiB that
has not been benchmarked against live streams.** Raise it empirically:

1. Raise it and `RADIO_LISTENER_MAX_SESSIONS` together by a small step.
2. Restart the planner and listener.
3. Watch for 30+ minutes: `queue_age_seconds` must stay flat, listener memory
   must stay well under its limit, and `station_sessions.last_error` must not
   fill with reconnects.
4. If any of those degrade, step back down. Record the result.

Ring-buffer memory is exactly predictable — `RADIO_RING_BUFFER_SECONDS ×
RADIO_SAMPLE_RATE × 2` bytes per station, ~1.83 MiB at the defaults — so
memory is rarely the binding constraint. ASR throughput usually is.

---

## 9. Backups

```bash
scripts/backup-sqlite.sh
```

Uses `sqlite3 .backup`, never `cp`. In WAL mode the database is several files
whose contents change between reads, so copying them individually can capture a
torn state that restores as a corrupt database — and you discover that at the
moment you most need it. The script verifies `PRAGMA integrity_check` on the
**copy** before keeping it.

Restore:

```bash
docker compose --profile core --profile pipeline down
gunzip -c /var/lib/radio/backups/radio-<stamp>.db.gz > /var/lib/radio/database/radio.db
chown 10001:10001 /var/lib/radio/database/radio.db
docker compose --profile core --profile pipeline up -d
```

Restore the database only. Do **not** restore the spool: segments referenced by
a restored database may be gone, and a stale spool is worse than an empty one —
the cleanup worker reconciles an empty spool correctly.

---

## 10. Deliberate limits

Things the system does not do, on purpose:

* **It does not delete audio by age alone.** Every deletion joins against job
  state. A `pending` segment is never removed at any watermark.
* **It does not send messages to a DLQ itself.** Redrive is a queue attribute.
  A message whose failure is understood is recorded in `processing_failures`
  and then deleted, so it does not burn `maxReceiveCount` for a known reason.
* **It does not let the LLM create mentions.** A mention exists because the
  matcher found a keyword. A wedged model produces a thinner mention, never a
  missing or invented one.
* **It does not transcribe with a translated transcript.** The original
  language is the evidence; translation is an additional field.
