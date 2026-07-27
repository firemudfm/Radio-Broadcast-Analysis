# ADR-004 — SQLite WAL, connection pragmas and write boundaries

Status: **Accepted** · Date: 2026-07-27

## Context

Today one SQLite file is opened by up to three processes (API, analysis worker,
reconciler). The Compose stack raises that to seven (`api`, `planner`,
`listener`, `transcription-worker`, `analysis-worker`, `cleanup-worker`, and
`migrate` on startup).

The audit found three concrete gaps (`CURRENT_ARCHITECTURE.md §3`):

* `PRAGMA synchronous` is **never** set — it stays at SQLite's `FULL` default,
  and because it is a **per-connection** setting, every new process must set it
  for itself.
* `PRAGMA busy_timeout` is not set as a pragma; the equivalent comes from
  `sqlite3.connect(timeout=30)`, which is per-connection too.
* There is no `SQLITE_BUSY` retry anywhere.

## Decision

### 1. Every connection, every process, no exceptions

A single `configure_connection()` helper is the only way a connection is
prepared, and it runs on **every** connection:

```sql
PRAGMA journal_mode = WAL;      -- persistent (database-level), still set every time
PRAGMA foreign_keys = ON;       -- per-connection
PRAGMA busy_timeout = 30000;    -- per-connection
PRAGMA synchronous = NORMAL;    -- per-connection  ← the gap being closed
```

`synchronous = NORMAL` with WAL is the documented durable-enough setting: a
crash cannot corrupt the database; only the last transactions can be lost on
*power* loss. That trade is correct for a pipeline whose upstream (SQS) will
redeliver anything not durably committed.

### 2. Versioned migrations

A `schema_migrations` table (`version INTEGER PRIMARY KEY`, `name`,
`applied_at_utc`, `checksum`) replaces ad hoc DDL for all **new** tables.
Migrations are ordered, idempotent, forward-only, and applied inside a single
transaction each. `checksum` detects a migration file edited after it was
applied.

The existing `db.py::SCHEMA` and `db_catalog.py::CATALOG_SCHEMA` are **not**
rewritten. They already run idempotently and are covered by the baseline suite;
the migration runner records them as baseline versions `0001` and `0002` when it
finds their tables already present. New tables start at `0003`.

### 3. Short transactions — a hard rule

Nothing that performs network, subprocess or model I/O may run inside
`Database.transaction()`. Specifically forbidden inside a transaction: opening a
radio stream, running FFmpeg, running Whisper, calling the LLM, uploading to S3,
receiving from SQS, or any other network wait.

This is enforced structurally rather than by convention: workers follow
*read → release → do slow work → short write*, and the outbox pattern
(ADR-009) exists precisely so that "commit state" and "send message" are two
separate steps instead of one long one.

The rule matters more than it looks: `Database.transaction()` holds a
process-wide `RLock` for the whole `with` block, so one slow transaction stalls
every thread in that process, not just the writer.

### 4. `SQLITE_BUSY` retry

`retry_on_busy()` wraps write transactions: up to `RADIO_SQLITE_BUSY_RETRIES`
(default 5) attempts with exponential backoff plus jitter, retrying only
`sqlite3.OperationalError` whose message contains `database is locked` or
`database is busy`. Every other error propagates immediately — a retry loop that
swallows real errors is worse than no retry loop.

### 5. Idempotency and uniqueness

| Constraint | Prevents |
|---|---|
| `UNIQUE(segment_id)` | duplicate segment rows |
| `UNIQUE(transcription_job_id)` | duplicate ASR jobs |
| `UNIQUE(analysis_job_id)` | duplicate analysis jobs |
| `UNIQUE(conversation_id)` | duplicate conversations |
| `UNIQUE(mention_id, campaign_id)` | duplicate campaign mapping |
| `UNIQUE(mention_id, keyword_id)` | duplicate keyword mapping |
| `UNIQUE(queue_name, message_deduplication_id)` | reprocessing a redelivered message |

Every one of these is a database constraint, not an application check.
Application checks race; `UNIQUE` does not.

### 6. Write ownership

| Table group | Writer |
|---|---|
| campaigns, keywords, campaign_stations | `api` |
| station_subscriptions, station_keyword_bindings, keyword_index_versions | `planner` |
| station_sessions, audio_segments | `listener` |
| transcription_jobs, transcripts | `transcription-worker` |
| conversation_sessions, mention_* | `transcription-worker` (assembler) |
| analysis_jobs, analysis_results | `analysis-worker` |
| outbox_events | producer writes, `planner` dispatches |
| inbox_messages | each consumer, for its own queue |
| worker_heartbeats | each worker, its own row |
| processing_failures | any component |

Single-writer-per-table is a design goal, not an enforced invariant — SQLite has
no row-level locking to give us that. It is documented so that lock contention
has an obvious owner when it appears.

## Alternatives considered

1. **PostgreSQL.** The technically correct answer for 7 writers, and the honest
   assessment is that it would remove this entire class of problem. Rejected
   *for this iteration* because it adds a managed dependency the pilot does not
   have, and every existing table, query and test targets SQLite. Recorded in
   ADR-008 as the migration trigger when write contention becomes the binding
   constraint — not as a hypothetical.
2. **One writer process, others via IPC.** Rejected: reimplements a database
   server badly.
3. **`synchronous = FULL`.** Rejected: an fsync per commit on EBS at this write
   rate is a measurable cost for durability that SQS redelivery already provides.
4. **`synchronous = OFF`.** Rejected: allows database corruption on OS crash.
5. **Separate database file per worker.** Rejected: cross-component queries
   (`mention → campaign → keyword`) are the product.

## Consequences

* Concurrent readers never block the writer and vice versa (WAL).
* Writers still serialise globally — SQLite has one write lock per database.
* A `-wal` and `-shm` file appear next to the database; backups must use
  `VACUUM INTO` or the backup API, never a raw file copy. `scripts/backup-sqlite.sh`
  does this correctly.
* Every container needs write access to the database *directory*, not just the
  file, because WAL creates sibling files.

## Operational risks

| Risk | Mitigation |
|---|---|
| WAL grows without bound | `wal_autocheckpoint` left at the default; cleanup worker runs `PRAGMA wal_checkpoint(TRUNCATE)` on a schedule |
| `SQLITE_BUSY` storms under load | Bounded retry with jitter; `database_busy_retries` exposed as a metric; short transactions keep the window small |
| A raw file-copy backup captures a torn state | `VACUUM INTO` only, asserted by a test on the script's command construction |
| Database on a container-local filesystem | Compose mounts `/var/lib/radio/database`; `/readyz` fails if the path is not writable |
| Migration applied by two containers at once | Migration runs in one transaction and takes the write lock; the loser sees the version already applied and no-ops |

## Security impact

`foreign_keys = ON` on every connection prevents orphaned mapping rows that
could otherwise expose a mention against a deleted campaign. The database file
and directory are `0640`/`0750`, owned by the container's non-root user. No SQL
is built by string interpolation of user input — the existing `# nosec B608`
sites interpolate only hard-coded column and predicate names, with all values
parameterised, and that pattern is retained.

## Cost impact

None. SQLite is a file. `synchronous=NORMAL` reduces EBS IOPS relative to the
current `FULL` default.

## Test requirements

* All four pragmas are set on **every** connection, asserted per-pragma.
* `PRAGMA synchronous` reports `1` (NORMAL) — a direct regression test for the
  audited gap.
* Migrations are idempotent: running twice is a no-op.
* An out-of-order or modified migration is detected via `checksum`.
* Foreign-key enforcement actually rejects an orphan insert.
* `SQLITE_BUSY` retry succeeds after a transient lock; a non-busy
  `OperationalError` is **not** retried.
* Concurrent short writes from multiple threads all commit.
* `PRAGMA integrity_check` passes after a concurrency soak.
* Every `UNIQUE` constraint above is exercised by an attempted duplicate.

## Reversal strategy

Migrations are forward-only by policy; each ships with a documented manual
`DOWN` in its docstring for emergency use. Because all new tables are additive
and legacy code never reads them, reverting the application to `d82d847` against
a migrated database works unchanged — the new tables are simply ignored.
