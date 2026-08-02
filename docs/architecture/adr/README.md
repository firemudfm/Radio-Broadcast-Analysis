# Architecture Decision Records

Each ADR records Context, Decision, Alternatives, Consequences, Operational
risks, Security impact, Cost impact, Test requirements and a Reversal strategy.

| ADR | Decision | The uncomfortable part |
|---|---|---|
| [single-pipeline](ADR-single-shared-sqs-pipeline.md) | Shared-station SQS is the ONLY production pipeline | One runtime to reason about, test, deploy and operate |
| ~~[001](ADR-001-legacy-and-shared-pipeline-modes.md)~~ | **SUPERSEDED** by [ADR-single-shared-sqs-pipeline](ADR-single-shared-sqs-pipeline.md) | The dual-mode switch existed until the shared pipeline was proven; it is now the only pipeline |
| [002](ADR-002-local-versus-s3-segment-storage.md) | `SegmentStore` abstraction; local spool by default, S3 for distributed mode | Disk becomes a first-class resource needing watermarks |
| [003](ADR-003-sqs-fifo-message-contracts.md) | FIFO queues, `MessageGroupId = station_id`, versioned Pydantic schemas | Per-group FIFO serialises a station's segments across the whole fleet — a real throughput ceiling |
| [004](ADR-004-sqlite-wal-and-write-boundaries.md) | WAL + four pragmas on **every** connection, versioned migrations, short transactions | `PRAGMA synchronous` was never set; PostgreSQL is the honest answer past ~100 stations |
| [005](ADR-005-audio-classification-policy.md) | Multi-signal classifier with hysteresis; **YAMNet not deployed** | Keras 2/3 conflict, no `tensorflow-cpu` aarch64 wheel, 269 MB full TF wheel on an 8 GiB host |
| [006](ADR-006-multilingual-asr-model-selection.md) | faster-whisper + CTranslate2, `small`/int8, two-pass | Defaults are safe starting points, **not** benchmark-derived optima |
| [007](ADR-007-local-qwen-runtime.md) | llama.cpp `b10144` + Qwen3-0.6B-Q8, JSON-schema + Pydantic + semantic validation | Whether this build honours `json_schema` on aarch64 is unverified; the client degrades safely |
| [008](ADR-008-one-thousand-station-scaling-boundary.md) | Capacity in **unique active stations**; deterministic blake2b sharding | One 4-vCPU host does **not** transcribe 1 000 live streams, and this repository never claims it does |
| [009](ADR-009-idempotency-and-outbox.md) | Transactional outbox + consumer inbox; delete the message **last** | SQS dedup lasts only 5 minutes, so the inbox — not SQS — is the guarantee |
| [010](ADR-010-song-advertisement-announcement-policy.md) | Two-stage classification; confirmation ladder; **the LLM never creates a mention** | No claim of perfect song/advert separation; accuracy must be measured, not asserted |

## Reading order

New to the project: [`../CURRENT_ARCHITECTURE.md`](../CURRENT_ARCHITECTURE.md) →
[`../TARGET_ARCHITECTURE.md`](../TARGET_ARCHITECTURE.md) → ADR-001 → ADR-008.

Implementing a worker: ADR-003 → ADR-009 → ADR-004.

Tuning quality: ADR-005 → ADR-006 → ADR-010 →
[`../../QUALITY_EVALUATION.md`](../../QUALITY_EVALUATION.md).
