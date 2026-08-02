# Target architecture — shared-station, SQS-driven radio intelligence

Companion documents: [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) (what
exists today), [`../research/TECHNOLOGY_RESEARCH.md`](../research/TECHNOLOGY_RESEARCH.md)
(verified versions and quotas), and [`adr/`](adr/) (decisions, alternatives and
reversal strategies).

---

## 1. The one idea

Work is de-duplicated at the **station** level, not the campaign level.

```
                       WRONG                              RIGHT
   3 campaigns × 1 station                     3 campaigns × 1 station
   → 3 connections                             → 1 connection
   → 3 transcriptions of the same audio        → 1 transcription
   → 3 LLM analyses of the same conversation   → 1 analysis
   → 3 mentions                                → 1 mention + 3 campaign mappings
```

Every structural choice below follows from that. `station_id` is the unit of
connection, the unit of the keyword index, the SQS `MessageGroupId`, the shard
key, and the conversation-assembly key.

---

## 2. Flow

```
Frontend
   │
   ▼
FastAPI control plane (:8788)              ─── unchanged public contract ───
   │            │
   │            └──► SQLite WAL ◄──── every component (short transactions only)
   ▼
Subscription planner
   • DISTINCT active station references (campaign-independent)
   • combined keyword index per station, content-versioned
   • capacity admission → active | pending_capacity
   • deterministic shard assignment: blake2b(station_id) % SHARD_COUNT
   │
   ▼
Shared multi-station listener  ── one async session per DISTINCT station ──
   • SSRF-validated connect, redirect re-validation on every hop
   • one FFmpeg subprocess per live stream → s16le 16 kHz mono on stdout
   • reconnect with exponential backoff + jitter; generation id per connect
   │
   ▼
Bounded RAM ring buffer (60 s, 1.92 MB/station)
   │
   ▼
AudioClassifier  (Silero VAD + energy/spectral + rolling hysteresis)
   │
   ├── silence / pure music / long-form singing ──► discard from RAM. Never written.
   │
   └── speech | speech_over_music | jingle | unknown ──► SegmentStore
                                                          ├── LocalSegmentStore  (default)
                                                          └── S3SegmentStore     (distributed)
                                                             │
                             transactional outbox ───────────┤
                                                             ▼
                                          SQS transcription.fifo  (metadata only)
                                          group = station_id, dedup = segment_id
                                                             │
                                                             ▼
                          Shared faster-whisper worker pool (inbox-deduplicated)
                          • pass A: cheap, high recall, language detection
                          • checksum verified before decode
                          • visibility heartbeat while decoding
                                                             │
                                                             ▼
                          Combined per-station keyword matcher (one index, all campaigns)
                             │                                    │
                        no candidate                        candidate
                             │                                    │
                    mark segment disposable            Conversation assembler
                    (cleanup worker deletes)           • 30 s pre-roll from ring metadata
                                                       • close on silence / music /
                                                         max duration / disconnect
                                                       • pass B confirmation for
                                                         fuzzy & phonetic candidates
                                                             │
                                            transactional outbox
                                                             ▼
                                          SQS analysis.fifo
                                          group = station_id, dedup = analysis_job_id
                                                             ▼
                                    Local Qwen analysis worker (one call per conversation)
                                    • JSON-schema constrained + Pydantic validated
                                    • evidence must exist verbatim in the transcript
                                    • circuit breaker → deterministic fallback
                                                             ▼
                                                      Result writer
                                     ┌───────────────────────┴────────────────────┐
                                     ▼                                            ▼
                          SQLite: 1 mention_event                    S3: mentions/YYYY/MM/DD/<id>/
                                  N mention_campaigns                    metadata.json
                                  M mention_keywords                     transcript.json
                                  1 analysis_result                      analysis.json
                                                                     evidence/YYYY/MM/DD/<id>.opus
```

---

## 3. Processes

| Service | Image | Count | Publishes | Owns |
|---|---|---|---|---|
| `api` | api | 1 | `8788:8788` | HTTP contract, campaign writes |
| `planner` | api | 1 | — | station subscriptions, keyword indexes, capacity, outbox dispatch |
| `listener` | pipeline | 1 per shard | — | station sessions, ring buffers, classification, segment writes |
| `transcription-worker` | pipeline | N (default 1) | — | ASR, matching |
| `analysis-worker` | pipeline | N (default 1) | — | conversation → LLM → result |
| `llm` | llm | 1 | **internal only**, `8790` | llama.cpp server |
| `cleanup-worker` | pipeline | 1 | — | spool retention, watermarks, stale-job recovery |

`api` and `planner` share the API image and differ only by entrypoint. No service
is per-station, per-campaign or per-keyword.

---

## 4. Reliability layers

| Layer | Mechanism | Table / setting |
|---|---|---|
| Transactional outbox | business state + outbox row in one SQLite transaction; dispatcher sends afterwards | `outbox_events` |
| Consumer idempotency | dedup on `(queue_name, message_deduplication_id)` committed with the business result | `inbox_messages` |
| Heartbeats | every worker writes liveness + role + shard | `worker_heartbeats` |
| Stale-job recovery | jobs `running` past a lease deadline return to `pending` | `transcription_jobs`, `analysis_jobs` |
| Visibility extension | background heartbeat calls `ChangeMessageVisibility` during long ASR/LLM work | `RADIO_SQS_VISIBILITY_*` |
| Checksum verification | SHA-256 recorded at write, verified before every read | `audio_segments.sha256` |
| Disk backpressure | warning → pause admission → emergency reclaim | `RADIO_SPOOL_*_PERCENT` |
| Error classification | retryable / permanent / invalid / missing / checksum / unsupported / db / exhaustion | `app/pipeline/errors.py` |
| Structured logs | one JSON object per line, ids only at INFO | `app/observability/logging.py` |
| Trace ids | generated at segment creation, carried through every message and row | `trace_id` |
| Model + schema versioning | pinned revisions with digests; every message carries `schema` | `models.lock.json` |

**Ordering of durability, non-negotiable:** commit the business result *and* the
inbox row in one SQLite transaction, **then** delete the SQS message. Never the
other way round.

---

## 5. Capacity vocabulary

The API distinguishes these and never conflates them:

| Field | Meaning | Scales to |
|---|---|---|
| `catalog_station_count` | Radio Browser + overlay records known | 1 000+ |
| `campaign_station_reference_count` | rows in campaign→station mappings | unbounded |
| `unique_requested_station_count` | DISTINCT stations wanted by active campaigns | 1 000+ |
| `unique_active_station_count` | DISTINCT stations with a live listener session | `RADIO_MAX_ACTIVE_UNIQUE_STATIONS` (default **8**) |
| `pending_capacity_station_count` | wanted but no compute slot | the remainder |
| `reused_station_stream_count` | stations whose reference count > 1 | proves de-duplication |
| `worker_count` | heartbeating workers by role | — |
| `transcription_queue_age_seconds` | oldest pending job age | primary saturation signal |
| `spool_usage_percent` | segment spool disk usage | backpressure trigger |

See `ADR-008` for the sharding path from 8 to 1 000 and why it needs no API change.

---

## 6. What stays exactly as it is

The **API contract**. Every existing route and response field is preserved,
including `active_station_limit`, which keeps its name and now reports the
shared-pipeline active limit. `auth_mode` stays `none`.

The **database**. Both pipelines always shared one schema, so removing the
legacy runtime was not a data-migration event: campaigns, stations, keywords
and mentions written by the old path remain readable and are planned as ordinary
rows. Historical tables are retained.

The **deployment stages** `api` / `core` / `full`. They are rollout stages, not
pipeline alternatives; `full` always runs this architecture.

What did NOT stay: `RADIO_PIPELINE_MODE`, the systemd unit templates,
`station_reconciler.py` and the legacy deploy scripts. See
`adr/ADR-single-shared-sqs-pipeline.md`.
