# Current architecture (baseline audit)

Audit target: commit `d82d84720e6f0e714308cbb830c7b66dafa15934` (`main`, v0.4.1).
Verified baseline on the packaging machine: **140 passed, 1 skipped** (`python -m pytest tests/ -q`),
matching the EC2 validation record in `VALIDATION.md`.

This document describes what the repository does *today*, before the shared SQS
pipeline work. It is a factual map, not a proposal. Nothing here is a
recommendation to change behaviour; the new pipeline is additive and gated
behind `RADIO_PIPELINE_MODE` (see `adr/ADR-001-legacy-and-shared-pipeline-modes.md`).

---

## 1. Repository inventory

```
app/
  __init__.py            version marker
  __main__.py            uvicorn entrypoint (python -m app)
  cli.py                 init-db / publish-keywords
  config.py              pydantic-settings Settings, ~90 validated fields
  db.py                  v0.3 SQLite schema + Database facade
  db_catalog.py          v0.4 catalogue/monitoring schema + CatalogStore
  main.py                FastAPI app factory + lifespan wiring
  models.py              brand-signal API models
  models_catalog.py      catalogue/monitoring API models
  analysis_worker.py     standalone shared analysis worker process
  station_reconciler.py  root-owned systemd/station reconciler
  s3_utils.py            S3 URI parsing + audio-key allowlist
  text.py                normalization / entity id
  api/routes.py          brand-signal routes
  api/catalog_routes.py  radio-catalog + monitoring routes
  services/              12 service modules (see §4)
  data/                  bundled curated Radio Browser overlay (249 KB + 16 KB)
deploy/                  systemd units, env examples, install/upgrade shell scripts
tools/                   overlay builder
tests/                   17 test modules, 141 tests
.github/workflows/       ci, deploy, health, rollback, codeql
```

There is **no** container, Compose file, migration framework, queue abstraction,
segment store, listener process, ASR code, or audio-classification code in the
repository today. Capture / upload / transcription runs in a *separate ingestion
package* that is installed on the EC2 host out-of-band; this repository only
consumes its S3 output.

---

## 2. API entrypoint, lifespan and dependency wiring

`app/main.py` builds a module-level `app = FastAPI(...)` with an
`asynccontextmanager` lifespan. Everything is constructed eagerly in the lifespan
and stashed on `app.state`:

| `app.state.*` | Type | Notes |
|---|---|---|
| `settings` | `Settings` | `get_settings()` is `lru_cache(maxsize=1)` |
| `database` | `Database` | one `sqlite3.Connection`, `check_same_thread=False` |
| `s3_client` | boto3 client | region from `effective_aws_region` |
| `station_service` | `StationService` | reads `/etc/radio-pipeline/stations/*.env` + S3 prefixes |
| `keyword_service` | `KeywordConfigService` | publishes `config/keywords/keywords.json` |
| `sync_service` | `IntelligenceSyncService` | owns a background `asyncio.Task` |
| `campaign_service` | `CampaignService` | orchestrates campaign writes |
| `audio_service` | `AudioService` | HMAC audio tokens + S3 range streaming |
| `conversation_service` | `ConversationService` | S3 transcript session assembly |
| `llm_client` | `LocalLlmClient` | `urllib` → `http://127.0.0.1:8790` |
| `analysis_service` | `MentionAnalysisService` | LLM analysis + S3 result cache |
| `catalog_store` | `CatalogStore` | v0.4 tables over the same connection |
| `radio_browser_client` | `RadioBrowserClient` | mirror discovery + caching |
| `catalog_service` | `CatalogService` | Radio Browser ⋈ curated overlay |
| `monitoring_service` | `MonitoringService` | capacity, admission, activation |
| `preview_service` | `PreviewService` | signed short-lived stream preview proxy |

There is **no** FastAPI `Depends()` graph — routes reach through
`request.app.state.<name>`. That is the existing convention and the new code
follows it rather than introducing a second style.

Lifespan startup order: logging → `Database.connect()` → boto3 → services →
`CatalogStore.migrate()` → `_load_bundled_overlay()` → `_import_legacy_stations()`
→ optional `sync_service.sync_once()` → `sync_service.start()`. Shutdown:
`await sync_service.stop()` → `database.close()`.

**Seam for the new work:** the lifespan is the single wiring point. Adding
pipeline-mode-conditional services there keeps `legacy` untouched when the flag
is default.

---

## 3. Database connection ownership and threading model

`app/db.py::Database` owns exactly one `sqlite3.Connection` created with
`check_same_thread=False, timeout=30`, guarded by a single process-wide
`threading.RLock`. Every read and write takes that lock.

```python
connection = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
connection.row_factory = sqlite3.Row
connection.executescript(SCHEMA)   # SCHEMA sets journal_mode=WAL, foreign_keys=ON
```

Observed facts and gaps:

* `PRAGMA journal_mode = WAL` and `PRAGMA foreign_keys = ON` are set inside the
  `SCHEMA` script, so they run once per connection at `connect()`.
* `PRAGMA busy_timeout` is **not** set explicitly; `sqlite3.connect(timeout=30)`
  provides the equivalent 30 s busy handler for this connection only.
* `PRAGMA synchronous` is **never** set — it stays at the SQLite default
  (`FULL`). `synchronous` is per-connection, so any new process must set it
  itself. This is a real gap the migration work must close, not inherit.
* `transaction()` issues `BEGIN IMMEDIATE`, yields the connection, commits, and
  rolls back on any exception. There is no `SQLITE_BUSY` retry loop.
* Async routes offload blocking DB work with `asyncio.to_thread(...)`.
  `IntelligenceSyncService` does the same. So multiple worker threads contend on
  the one `RLock`, serialising all SQLite access inside the API process.
* Separate **processes** (`app.analysis_worker`, `app.station_reconciler`) open
  their *own* `Database` against the same file. Cross-process concurrency
  therefore relies on WAL + the 30 s busy timeout, not the in-process lock.

**Migration mechanism today:** ad hoc. `db.py::_migrate` inspects
`PRAGMA table_info(campaign_keywords)` and issues `ALTER TABLE ... ADD COLUMN`
for three known columns. `db_catalog.py::migrate()` runs a second
`CREATE TABLE IF NOT EXISTS` script. There is no `schema_migrations` table and no
version ordering. This is precisely the "ad hoc CREATE TABLE scattered across
modules" pattern the new work must replace — additively, without breaking the
existing idempotent DDL.

---

## 4. Service map

| Module | Responsibility | External I/O |
|---|---|---|
| `services/stations.py` | Enumerate pipeline stations from `*.env` files + S3 `raw-audio/` prefixes; 60 s cache | filesystem, S3 |
| `services/campaigns.py` | Campaign CRUD orchestration, station-id validation, hydration | via Database/S3 |
| `services/keywords.py` | Build and PUT the shared `keywords.json` entity document | S3 |
| `services/sync.py` | Poll `results/intelligence/`, match mentions to campaign bindings, materialise `mentions` rows | S3 |
| `services/conversation.py` | Load neighbouring `*.transcript.json` from S3, assemble a contiguous speech session, compute char/timestamp highlights | S3 |
| `services/analysis.py` | Per-mention LLM analysis with an S3 result cache keyed by transcript SHA-256 | S3, LLM |
| `services/llm.py` | llama.cpp OpenAI-compatible client; `analyze()` and `match_keyword()` | HTTP localhost |
| `services/semantic.py` | Cross-language keyword discovery over transcript groups; exact-first, LLM second | S3, LLM |
| `services/audio.py` | HMAC-SHA256 audio tokens; S3 byte-range streaming | S3 |
| `services/preview.py` | Signed preview token + SSRF-safe streaming proxy with byte/duration caps | HTTP upstream |
| `services/radio_browser.py` | Radio Browser mirror discovery, retry, caching | HTTP |
| `services/catalog.py` | Merge Radio Browser results with the curated override/deletion overlay | via store/client |
| `services/monitoring.py` | Capacity snapshot, admission control, selection estimate, activation lifecycle | `/proc/meminfo`, `os.getloadavg` |
| `services/net_safety.py` | SSRF guard: scheme/userinfo/port checks, DNS resolution, global-unicast-only | DNS |

---

## 5. Campaign write flow (exact sequence)

`POST /api/v1/brand-signal/campaigns` → `routes.create_campaign`:

1. `CampaignService.create_campaign(payload)`
   * `StationService.station_map()` — validates every legacy `station_ids` entry exists.
   * `monitor_from = now - backfill_days`.
   * `Database.create_campaign()` — one `BEGIN IMMEDIATE` transaction writing
     `campaigns`, `campaign_stations`, `campaign_keywords`, then
     `_increment_revision()` bumping `app_meta.campaign_revision`.
   * `KeywordConfigService.publish()` — GET + PUT `config/keywords/keywords.json`.
   * `IntelligenceSyncService.sync_once()` — full S3 result rescan.
2. If `payload.station_selection` is present:
   * `MonitoringService.attach_campaign_selection()` — resolves the selection,
     writes `campaign_station_rules`, upserts `managed_stations`, sets
     `campaign_station_members`, recomputes reference counts, then queues
     `activate` jobs up to the free-slot count and parks the rest in
     `pending_capacity`.
   * On `MonitoringError`/`RadioBrowserError` the campaign is **deleted** and the
     error is surfaced (compensating action, not a transaction).
   * `CatalogStore.add_campaign_station_ids()` bridges `rb-<uuid>` local ids into
     the legacy `campaign_stations` table so `sync` attributes mentions.
   * `bump_campaign_revision()`.

**Station-reference logic** already exists and is the correct precedent for the
new planner: `CatalogStore.recompute_reference_counts(stop_grace_seconds=...)`
counts active-campaign references per managed station, arms/disarms
`stop_after_utc` on 1→0 and N→1 transitions, and revives referenced-but-stopped
stations. `RADIO_MAX_ACTIVE_STATIONS` (1..64, default 2) is the hard admission
limit; overflow parks in `pending_capacity` and `_promote_pending_capacity()`
promotes when a slot frees.

Note the existing counter is **campaign-reference count per managed station**,
i.e. it already models station sharing at the catalogue layer. What it does not
yet do is build a *combined keyword index per station* — keyword publication is
global (`keywords.json` with one entity list), not per-station.

---

## 6. Keyword publishing

`KeywordConfigService.publish()` reads the existing `config/keywords/keywords.json`
from S3, preserves every entity whose `managed_by != "firemud-radio-api-open"`,
regenerates the managed entities from `Database.active_bindings()`, and PUTs the
whole document back with `ServerSideEncryption="AES256"`.

Managed entity shape:

```json
{
  "id": "api-<slug>-<sha256[:10]>",
  "display_name": "NVIDIA",
  "entity_type": "brand",
  "enabled": true,
  "match_mode": "tokens",
  "aliases": {"*": ["NVIDIA", "एनवीडिया"]},
  "managed_by": "firemud-radio-api-open",
  "campaign_ids": ["..."],
  "station_ids": ["..."]
}
```

Aliases are a flat list under the `"*"` language bucket — there is **no**
per-language, per-kind (native script / romanization / ASR variant) structure
today. `entity_id_for()` derives ids from NFKD-casefolded, token-joined text.

---

## 7. Conversation reconstruction (current, S3-based)

`ConversationService.build(mention)` is read-time, not stream-time:

1. Take `mention.transcript_s3_key`; require the `transcripts/` prefix.
2. For the canonical key layout
   `transcripts/<station>/<YYYY>/<MM>/<DD>/<source-chunk>/<segment>.transcript.json`,
   paginate the whole day prefix, group by parent "chunk" directory, and take
   `RADIO_CONVERSATION_SCAN_CHUNKS` (6) groups either side of the anchor.
3. Load up to `RADIO_CONVERSATION_MAX_TRANSCRIPTS` (200) documents, sort by
   broadcast start.
4. `_select_contiguous_session()` walks outward from the anchor while the gap to
   the neighbour is ≤ `RADIO_CONVERSATION_SESSION_GAP_SECONDS` (30) and the total
   span is ≤ `RADIO_CONVERSATION_MAX_DURATION_SECONDS` (1800).
5. Concatenate word-level text, tracking char offsets, producing
   `full_transcript`, `transcript_segments`, `words`, `highlights`.
6. Highlight strategy: timestamp overlap first, then exact/normalized alias
   search (`find_normalized_span`, NFKD + combining-mark stripping + alnum-only).

Cost profile: one `list_objects_v2` paginate over a day prefix plus up to 200
`get_object` calls **per mention detail request**. This is the main reason the
new pipeline assembles conversations at stream time instead.

---

## 8. Analysis flow and LLM request format

`MentionAnalysisService`:

* Cache key: `results/conversation-analysis/<station>/<YYYY>/<MM>/<DD>/<mention-id>.analysis.json`.
* Cache validity is checked against `transcript_sha256` — a SHA-256 of
  `full_transcript`. A changed transcript invalidates the cache.
* On miss: set status `pending` (+attempts), call the LLM, PUT the document with
  `ServerSideEncryption="AES256"`, set status `ready`, and push
  sentiment/confidence/needs_review back into the `mentions` row.
* `mention_analysis` tracks `status ∈ {pending, ready, disabled, error}` and
  `attempts`.

`LocalLlmClient` posts to `POST {base}/v1/chat/completions` with:

```json
{
  "model": "qwen3-0.6b-q8", "temperature": 0.1, "top_p": 0.8, "top_k": 20,
  "presence_penalty": 1.0, "max_tokens": 480, "stream": false,
  "response_format": {"type": "json_object"},
  "messages": [{"role": "system", "...": "..."}, {"role": "user", "...": "/no_think ..."}]
}
```

Non-thinking mode is requested with a literal `/no_think` prefix in the user
message (Qwen3 soft switch). Output handling is *lenient*: strip ``` fences,
`json.loads`, else regex the first `{...}` block. Enum values are clamped to
allowlists. Evidence strings are kept only if `value.casefold() in transcript.casefold()`.
There is **no** JSON-schema-constrained decoding (`response_format: json_schema`
/ GBNF grammar) and no Pydantic validation of the model output.

Retry/timeout behaviour: `urllib` with `RADIO_LLM_TIMEOUT_SECONDS` (90). One
attempt. No circuit breaker. Failure → `mention_analysis.status='error'` and the
worker retries while `attempts < RADIO_ANALYSIS_RETRY_LIMIT` (3).

---

## 9. Current job and retry behaviour

Two independent polling loops, no queue:

**`app/analysis_worker.py`** (`radio-analysis-worker.service`)
```
loop:
  semantic.scan_once()                      # bounded: 1 group/cycle, 10 keywords/group
  ids = db.list_pending_analysis(limit=2, retry_limit=3, settle_seconds=360)
  for id in ids: service.analyze(id, force=False)
  sleep(0.2) or wait(poll=20s) when idle
```
Claiming is **not** atomic — `list_pending_analysis` is a plain SELECT. A second
worker process would double-analyse. Today exactly one unit is installed, so the
invariant holds by deployment, not by design.

**`app/station_reconciler.py`** (`radio-station-reconciler.service`, root)
```
loop:
  _schedule_due_stops()          # recompute refs, arm/disarm stop timers
  _promote_pending_capacity()    # fill free slots from pending_capacity
  job = store.claim_next_job()   # BEGIN IMMEDIATE; refuses if any job is 'running'
  dispatch(probe|reprobe|activate|stop)
```
`claim_next_job()` *is* atomic (single `BEGIN IMMEDIATE`, global "at most one
running job" rule). Jobs are terminal on completion or failure — there is no
backoff, no attempt cap, and no dead-letter path; a failed job leaves the station
in `failed_probe`/`degraded` with `last_error` set.

---

## 10. Station lifecycle (today)

```
available → pending_probe → probing → (failed_probe)
                                   ↘ available            (probe-only)
                                   ↘ activating → active → degraded
pending_capacity ──promotion──→ pending_probe
active/degraded → stopping → stopped
```

Activation writes two files and enables three systemd template units:

* `/etc/radio-pipeline/stations/<id>.env` — identity keys replaced, all other
  keys copied from the installed `hertz879.env` template.
* `/etc/radio-pipeline/automation/<id>.env` — written once, never overwritten.
* `radio-capture@<id>`, `radio-uploader@<id>`, `radio-pipeline-worker@<id>`.

Guards: `_STATION_ID_RE = ^rb-[0-9a-f-]{8,64}$` refuses any non-`rb-` id for both
activate and stop; legacy-pinned stations can never be stopped.

---

## 11. S3 object layout (current)

| Prefix | Written by | Read by |
|---|---|---|
| `raw-audio/<station>/YYYY/MM/DD/` | external ingestion | `StationService._s3_station_ids` |
| `clean-speech/...` | external ingestion | `AudioService` (**only** allowed audio prefix) |
| `transcripts/<station>/YYYY/MM/DD/<chunk>/<seg>.transcript.json` | external ingestion | `ConversationService`, `SemanticDiscoveryService` |
| `results/intelligence/*.json` | external ingestion | `IntelligenceSyncService` |
| `results/conversation-analysis/<station>/YYYY/MM/DD/<mention>.analysis.json` | `MentionAnalysisService` | itself (cache) |
| `results/semantic-matches/<station>/YYYY/MM/DD/<marker>.semantic.json` | `SemanticDiscoveryService` | audit only |
| `config/keywords/keywords.json` | `KeywordConfigService` | external ingestion |

`s3_utils.is_allowed_audio_key()` is the hard security boundary for playback:
a key must start with `clean-speech/` and must not end with `/`. Raw audio is
never streamable through the API.

All API-written objects use `ServerSideEncryption="AES256"`. No presigned URLs
are generated anywhere; playback proxies bytes through FastAPI behind an HMAC
token.

---

## 12. Existing SSRF safeguards

`services/net_safety.py::validate_public_http_url` fails closed:

* scheme ∈ {http, https} only; no userinfo; port 1..65535.
* IP literals — including decimal (`2130706433`) and hex (`0x7f000001`) forms —
  are parsed without DNS.
* Hostnames resolve via `getaddrinfo(AF_UNSPEC, SOCK_STREAM)`; **every** returned
  address must be global unicast. One private answer rejects the whole set
  (DNS-rebinding-resistant at validation time).
* IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is unwrapped and the inner address is
  validated too.
* Rejects loopback, link-local, multicast, unspecified, reserved, private,
  and anything where `is_global` is false.
* `MAX_REDIRECTS = 5`; both `PreviewService.open_stream` and
  `Reconciler._resolve_stream_url` re-validate on **every** hop and never let the
  HTTP library follow redirects itself.

`tests/test_net_safety.py` (9 KB) already covers loopback, RFC1918, link-local
metadata, IPv6 loopback/ULA/v4-mapped, decimal/hex literals, and mixed
public+private DNS answers. **This module and its tests are preserved verbatim.**

---

## 13. Audio-token security

Two independent HMAC-SHA256 token schemes, both keyed on
`RADIO_AUDIO_TOKEN_SECRET` (validated ≥ 32 chars):

| | Mention audio (`services/audio.py`) | Station preview (`services/preview.py`) |
|---|---|---|
| Payload | `{mention_id, audio_key, exp}` | `{station_uuid, exp, kind:"preview"}` |
| Encoding | base64url(JSON) + `.` + base64url(HMAC) | same |
| TTL | `RADIO_AUDIO_TOKEN_TTL_SECONDS` (30..3600, default 600) | `RADIO_PREVIEW_TOKEN_TTL_SECONDS` (120) |
| Verify | `hmac.compare_digest`, exp check, **re-check `is_allowed_audio_key`** | `compare_digest`, `kind`, exp |
| Caps | HTTP Range validated against `ContentLength` | 60 s wall clock, 32 kB/s × 60 s byte cap, 2 concurrent |

Re-validating the audio key at *verify* time (not just at issue time) means a
token cannot be replayed to reach a non-`clean-speech/` object even if the DB row
changed. That property is retained.

---

## 14. Subprocess calls and writable paths

**Every** subprocess call in the repository (all in `station_reconciler.py`):

```python
subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
```
* `shell=True` is never used; commands are explicit argument arrays.
* `["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", "--max-time", "8", "--no-location", url]` — timeout 12 s
* `["runuser", "-u", "radio", "--", "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-select_streams", "a:0", "-analyzeduration", "10M", "-user_agent", UA, "-i", url]` — timeout `RADIO_PROBE_SECONDS + 10`
* `["systemctl", "enable"|"disable"|"is-active", "--now"|"--quiet", unit]` — 30–90 s

There is **no** `TimeoutExpired` → `kill()` escalation and no process-group
cleanup; `subprocess.run` kills the direct child on timeout only. Any new
long-lived FFmpeg supervision must add explicit termination escalation.

Writable paths today:

| Path | Writer | Mode |
|---|---|---|
| `RADIO_DATABASE_PATH` (default `/var/lib/firemud/radio-intelligence-api/radio.db`) | API, analysis worker, reconciler | `mkdir(parents=True)` at connect |
| `/etc/radio-pipeline/stations/<id>.env` | reconciler (root) | `0640` |
| `/etc/radio-pipeline/automation/<id>.env` | reconciler (root) | `0640`, write-once |

No spool, model, evidence, log, or backup directory exists yet.

---

## 15. Deployment assumptions (current)

* Amazon Linux 2023, systemd, `radio` system user, venv at
  `/opt/firemud/radio-intelligence-api/venv`.
* Config at `/etc/firemud/radio-intelligence.env` (single env file).
* llama.cpp built from source at tag `b10034` into
  `/opt/firemud/llm-runtime/bin/llama-server`; model
  `/opt/firemud/llm-runtime/models/Qwen3-0.6B-Q8_0.gguf` downloaded at install
  time and `sha256sum`-recorded (recorded, not *verified against a pin*).
* LLM bound to `127.0.0.1:8790`, `MemoryMax=3G`, `CPUQuota=400%`, `Nice=10`,
  `--sleep-idle-seconds`.
* API `MemoryMax` unset; analysis worker `MemoryMax=768M`, `CPUQuota=150%`.
* CI: ruff → pytest (3.11 + 3.12) → bandit `-ll` → pip-audit.
  CD: SSH + rsync + `deploy/ci-deploy.sh`, health-checked with automatic rollback.
* Ingestion (capture/uploader/pipeline-worker) is **not** in this repository.

---

## 16. Test seams that already exist

| Seam | How it is injected | Used by |
|---|---|---|
| `FakeS3` | constructor arg `s3_client` on every service | most tests |
| `CommandRunner` | `Reconciler(runner=...)` | `test_reconciler.py` |
| `Resolver` | `validate_public_http_url(resolver=...)` | `test_net_safety.py` |
| `opener_factory` | `PreviewService(opener_factory=...)` | preview tests |
| `stations_dir` / `automation_dir` / `template_station_id` | `Reconciler(...)` | reconciler tests |
| `Settings(...)` | direct kwargs, `tmp_path` fixtures | `conftest.py` |

Every external dependency is already constructor-injected. The new code uses the
same pattern — no monkeypatching, no import-time singletons beyond
`get_settings()`.

---

## 17. Gaps this baseline has, relative to the target

Stated plainly, because the new design exists to close them:

1. **No queue.** Work is discovered by polling S3 and SQLite. `list_pending_analysis`
   is not an atomic claim.
2. **No outbox.** SQLite state and external side effects (S3 PUT, keyword publish)
   are not linked; a crash between them silently diverges.
3. **No inbox / idempotency table.** Redelivery protection today is
   `UNIQUE(campaign_id, source_result_s3_key, source_mention_id)` on `mentions`
   plus `sync_objects` etag checks — per-source, not per-message.
4. **Mention is per (campaign, keyword).** `mentions` carries `campaign_id` and
   `campaign_keyword_id` **columns**, so one physical broadcast moment that
   matches three campaigns creates three rows, three analyses, three LLM calls.
   The target requires one mention + mapping tables.
5. **No per-station keyword index.** `keywords.json` is one global entity list.
6. **`PRAGMA synchronous` never set**; no `busy_timeout` pragma; no `SQLITE_BUSY` retry.
7. **No schema_migrations table.**
8. **No heartbeats, no trace ids, no structured JSON logs** (one-off `json.dumps`
   event lines in the reconciler are the only structured output).
9. **No `/readyz`**; `/healthz` performs an S3 `list_objects_v2` and an LLM HTTP
   call on every request — not lightweight.
10. **No model pinning/verification** beyond a post-hoc `sha256sum` file.
11. **Long-running work inside a lock**: `Database.transaction()` holds the
    process RLock for the whole `with` block; today those blocks are short, and
    that must remain true.
12. **No LLM output schema enforcement**, no repair retry, no circuit breaker.

---

## 18. Invariants the new work must not break

These are load-bearing and are asserted by the existing 140-test suite:

* `is_allowed_audio_key()` remains the only gate for playable audio.
* `validate_public_http_url()` is called before every outbound stream connection
  and on every redirect hop.
* Public API responses never contain `stream_url_resolved`, S3 URIs, or credentials.
* `auth_mode="none"` and `storage_mode="sqlite"` literals stay in `HealthView`
  and `DashboardView`.
* Legacy-pinned stations are never stopped.
* `_STATION_ID_RE` guards every systemd unit name interpolation.
* `ApiModel` uses `extra="forbid"` — additive response fields need model changes,
  and additive *request* fields must be optional with defaults.
* `CampaignCreate` accepts legacy `station_ids` **or** `station_selection`.
* `mentions` keeps `UNIQUE (campaign_id, source_result_s3_key, source_mention_id)`.
