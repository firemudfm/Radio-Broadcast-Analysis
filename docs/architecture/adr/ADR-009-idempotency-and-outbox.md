# ADR-009 — Transactional outbox and consumer idempotency

Status: **Accepted** · Date: 2026-07-27

## Context

SQLite and SQS cannot participate in one atomic transaction. Any code that
commits a database row and then sends a message has two failure windows:

* crash **after commit, before send** → the job exists but is never queued
  (silent stall);
* crash **after send, before recording the send** → resend on restart
  (duplicate).

The current codebase has the same problem in a milder form: `create_campaign`
commits, then PUTs `keywords.json`, then runs a sync. A crash between them leaves
S3 and SQLite disagreeing, and nothing detects or repairs it.

## Decision

### 1. Transactional outbox

Producers never call SQS directly. They write business state **and** an
`outbox_events` row in **one** SQLite transaction:

```
BEGIN IMMEDIATE
  INSERT INTO transcription_jobs (...)         -- business state
  INSERT INTO outbox_events (
     event_id, queue_name, message_group_id,
     message_deduplication_id, payload_json,
     status='pending', attempts=0, available_at_utc=now)
COMMIT
```

A dispatcher (in the `planner` process) then:

1. claims a batch of `pending` rows whose `available_at_utc` has passed, in one
   short transaction, marking them `sending` with a lease;
2. releases the transaction, **then** calls SQS (network I/O outside any
   transaction — ADR-004);
3. records `sent` with `sqs_message_id` and `sent_at_utc`;
4. on failure, sets `available_at_utc = now + backoff(attempts)` with jitter and
   returns the row to `pending`;
5. after `RADIO_OUTBOX_MAX_ATTEMPTS` (default 10), marks `failed` and writes a
   `processing_failures` row. Failed rows are never deleted automatically —
   they are evidence.

**Resend is safe** because `message_deduplication_id` is stable and derived from
the business key (`segment_id`, `analysis_job_id`), so SQS deduplicates within
its 5-minute window and the consumer inbox deduplicates beyond it.

Stale `sending` rows (lease expired — a dispatcher crashed mid-send) return to
`pending`. That may resend a message that was actually delivered; that is
precisely the case the inbox exists for.

### 2. Consumer inbox

Every consumer follows the same six steps, in this order:

1. receive
2. validate schema (invalid → permanent, record + delete)
3. check `inbox_messages` for `(queue_name, message_deduplication_id)` → if
   present and `status='processed'`, delete the SQS message and stop
4. process idempotently (slow work — **outside** any transaction)
5. commit business result **and** the `inbox_messages` row **in one SQLite
   transaction**
6. **only then** delete the SQS message

**Step 6 never moves before step 5.** If the process dies between 5 and 6, the
message is redelivered and step 3 makes it a no-op. If it dies between 4 and 5,
the work is redone — which is why step 4 must be idempotent, and why every
business insert is guarded by a `UNIQUE` constraint (ADR-004 §5) rather than a
prior `SELECT`.

### 3. Error classification

| Class | Handling |
|---|---|
| `RetryableError` | leave the message; let visibility expire; SQS redelivers |
| `PermanentError` | record in `processing_failures`, write the inbox row, delete the message |
| `InvalidMessageError` | record, delete (a malformed message will never become valid) |
| `SegmentMissingError` | permanent — the audio is gone |
| `ChecksumMismatchError` | permanent — quarantine the file, record |
| `UnsupportedModelError` | permanent for this consumer version |
| `DatabaseUnavailableError` | retryable |
| `ResourceExhaustedError` | retryable with extended backoff |

The application **never** sends to a DLQ. Redrive is a queue attribute
(`maxReceiveCount`); deleting a message whose failure we already understand
prevents it burning retries for a known reason.

### 4. Stale-job recovery

Jobs carry `lease_expires_at_utc`. A sweeper in `cleanup-worker` returns
`running` jobs past their lease to `pending` and increments `attempts`. Past
`RADIO_JOB_MAX_ATTEMPTS` the job is `failed` with a recorded reason. This catches
the case SQS cannot: a worker that was killed after `ReceiveMessage` succeeded
but before any progress was durable.

### 5. Heartbeats

Every worker upserts `worker_heartbeats(worker_id, role, shard_index,
last_seen_utc, status, detail_json)` on a fixed interval. `/healthz` reports a
role as `stale` when its newest heartbeat exceeds
`RADIO_HEARTBEAT_STALE_SECONDS` (default 120).

## Alternatives considered

1. **Send directly, accept the window.** Rejected: silent stalls are the worst
   failure mode — nothing errors, work simply never happens.
2. **Send first, then commit.** Rejected: guarantees duplicates on every
   producer crash and makes the queue the source of truth for state it does not
   own.
3. **Rely only on SQS FIFO deduplication.** Rejected: the deduplication interval
   is **5 minutes** (verified). A retry after a 10-minute outage would duplicate.
   SQS dedup is defence in depth; the inbox is the guarantee.
4. **Rely only on `UNIQUE` constraints (no inbox).** Partially sufficient —
   uniqueness does prevent duplicate rows — but it cannot distinguish "already
   processed" from "concurrent conflict", it produces exception-driven control
   flow, and it gives no audit trail of what was consumed when.
5. **Change data capture / SQLite triggers.** Rejected: opaque and hard to test
   compared with an explicit table.

## Consequences

* At-least-once delivery with exactly-once **effect**.
* One extra table write per produced message and per consumed message.
* `outbox_events` and `inbox_messages` grow; retention is configurable
  (`RADIO_OUTBOX_RETENTION_DAYS`, `RADIO_INBOX_RETENTION_DAYS`, default 7) and
  pruned by `cleanup-worker` — but `failed` outbox rows are never auto-pruned.
* Producers do not need SQS credentials; only the dispatcher does. That is a
  useful blast-radius reduction.
* Message latency gains one dispatcher poll interval
  (`RADIO_OUTBOX_POLL_SECONDS`, default 2).

## Operational risks

| Risk | Mitigation |
|---|---|
| Dispatcher down → nothing is sent | `worker_heartbeats` + `/healthz`; `outbox_pending_count` and `outbox_oldest_pending_seconds` metrics |
| Outbox backlog after an SQS outage | Bounded backoff with jitter; the queue drains on recovery; the metric shows depth |
| Inbox table growth | Pruned on retention, indexed on `(queue_name, message_deduplication_id)` |
| Duplicate work between step 4 and step 5 | Idempotent processing + `UNIQUE` constraints; the wasted CPU is accepted |
| Lease sweeper too aggressive | Lease default (300 s) is above the p99 job duration and is configurable |
| Clock skew across containers | All containers share the host clock; leases use UTC and generous margins |

## Security impact

* Only the dispatcher holds SQS send permission — least privilege by process.
* `payload_json` in the outbox contains no secrets (ADR-003 forbids them in
  messages), so the table is not a credential store.
* `processing_failures` stores a bounded, truncated error string, never a full
  transcript or a stack trace with paths.
* The inbox is an audit trail of every message consumed, which is useful during
  incident review.

## Cost impact

Negligible: extra SQLite writes. Resends after a dispatcher crash may cost a few
duplicate SQS sends, deduplicated by SQS within 5 minutes.

## Test requirements

* **Crash before send:** commit job + outbox, restart, dispatcher sends it. No
  lost work.
* **Crash after send, before marking sent:** restart resends; consumer inbox
  makes it a no-op; exactly one business row exists.
* **Duplicate delivery** of an already-processed message produces no second row
  and deletes the message.
* Message is deleted **only after** the business commit (asserted by ordering a
  spy on both).
* Backoff grows and is jittered; `available_at_utc` is respected.
* `RADIO_OUTBOX_MAX_ATTEMPTS` exceeded → `failed` + `processing_failures`, not
  an infinite loop.
* Stale `sending` lease returns the row to `pending`.
* Stale `running` job lease returns the job to `pending`.
* Each error class routes to the correct handling (parametrised).
* No code path calls `SendMessage` to a DLQ (a static test over the source).
* Heartbeat staleness surfaces in `/healthz`.

## Reversal strategy

`RADIO_OUTBOX_ENABLED=false` makes producers send inline (the naive path),
retained only for debugging and logged as a WARNING on every send. The inbox
cannot be disabled — it is the correctness guarantee, and a flag that turns off
correctness is not a feature.
