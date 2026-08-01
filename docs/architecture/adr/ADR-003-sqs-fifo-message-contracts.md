# ADR-003 — SQS FIFO message contracts

Status: **Accepted** · Date: 2026-07-27

## Context

Two hand-offs need durable, ordered, exactly-once-effect delivery:
listener → transcription, and matcher → analysis. Verified SQS quotas and FIFO
semantics are recorded in
[`../../research/TECHNOLOGY_RESEARCH.md §3`](../../research/TECHNOLOGY_RESEARCH.md).

The load-bearing fact, quoted from the SQS Developer Guide
(`FIFO-queues-understanding-logic`):

> "No additional messages from the same message group ID are returned until the
> first message is deleted or becomes visible again."

## Decision

### Queues

| Queue | Type | `MessageGroupId` | `MessageDeduplicationId` |
|---|---|---|---|
| `radio-transcription.fifo` | FIFO | `station_id` | `segment_id` |
| `radio-analysis.fifo` | FIFO | `station_id` | `analysis_job_id` |

**`MessageGroupId = station_id` is chosen deliberately, accepting that it
serialises a station's segments across the entire worker fleet.** Segments must
enter the conversation assembler in broadcast order; out-of-order ASR results
would produce corrupted transcripts and mis-timed evidence. Parallelism comes
from having many stations, not from racing one station's segments.

The throughput consequence is arithmetic, and is stated rather than hidden:

```
sustainable_unique_stations ≈ worker_concurrency × (segment_seconds / per_segment_latency)
```

This is the primary input to `RADIO_MAX_ACTIVE_UNIQUE_STATIONS` (default 8) and
is why `transcription_queue_age_seconds` is the headline saturation metric.

Analysis uses `station_id` rather than `conversation_id` for the same reason:
conversations from one station are close in time and share the single-threaded
LLM; per-conversation groups would let a later conversation's analysis overtake
an earlier one for the same station, producing out-of-order dashboards for no
throughput gain (the LLM is one shared server anyway).

### Versioned schemas

Every message carries `schema`. Consumers accept an explicit allowlist and reject
anything else as `InvalidMessage` (permanent — recorded and deleted, not retried).

`radio.transcription.v1`:

```json
{
  "schema": "radio.transcription.v1",
  "job_id": "uuid", "segment_id": "uuid",
  "station_id": "string", "station_session_id": "uuid",
  "sequence_number": 123,
  "started_at": "ISO-8601 UTC", "duration_ms": 20000,
  "content_class": "speech_over_music",
  "language_hints": ["hi", "en"],
  "keyword_index_version": 42,
  "storage": {"backend": "local", "path": "…", "bucket": null, "key": null,
              "sha256": "…", "size_bytes": 123456},
  "trace_id": "uuid", "created_at": "ISO-8601 UTC"
}
```

`radio.analysis.v1`:

```json
{
  "schema": "radio.analysis.v1",
  "analysis_job_id": "uuid", "mention_id": "uuid", "conversation_id": "uuid",
  "station_id": "string", "language": "hi",
  "transcript_reference": {"backend": "sqlite", "transcript_id": "uuid"},
  "matched_keywords": [{"keyword_id": "uuid", "campaign_ids": ["uuid"],
                        "canonical_value": "NVIDIA", "matched_text": "एनवीडिया",
                        "match_level": "alias", "start_char": 44, "end_char": 52,
                        "start_ms": 32000, "end_ms": 33300, "confidence": 0.95}],
  "campaign_ids": ["uuid"],
  "trace_id": "uuid", "created_at": "ISO-8601 UTC"
}
```

### `matched_keywords` is evidence, not a pointer

`MatchedKeywordRef` must carry everything the result writer persists into
`mention_keywords`, because the analysis worker cannot recompute any of it: it
never sees the audio, the per-segment transcript, or the station's keyword
index.

The first implementation carried only `keyword_id`, `canonical_value`,
`matched_text`, `start_ms` and `end_ms`. The consumer filled the rest with
constants — `match_level="exact"`, `confidence=1.0`, `start_char=0`, and *every*
conversation campaign on *every* keyword. Those constants were not defaults,
they were fabrications, and they landed in the permanent audit trail: an alias
hit was recorded as `exact`, a candidate was recorded as `confirmed`, and
per-keyword attribution was lost.

`campaign_ids`, `match_level`, `start_char`, `end_char` and `confidence` are
therefore part of the record. They were added **additively** to
`radio.analysis.v1`, each with a documented default, so a message serialised
before they existed still parses rather than becoming a poison message. The
schema string is deliberately unchanged: optional additive fields do not
justify a v2 contract, and a v2 would strand every message already queued.

**Two campaign lists, two meanings.** `AnalysisJobV1.campaign_ids` is every
campaign the physical conversation belongs to. `MatchedKeywordRef.campaign_ids`
is only the campaigns owning that specific keyword. The second is a subset of
the first, and collapsing them is the defect described above.

**Coordinates.** `start_char`/`end_char` index into the conversation's assembled
`transcript_text`; `start_ms`/`end_ms` are measured from the start of the
conversation. The matcher works in per-segment coordinates, so
`ConversationAssembler` rebases both when the conversation closes. This matches
the convention the legacy transcript API already uses
(`app/services/conversation.py` slices the assembled transcript by these
offsets), so the frontend highlight contract is unchanged.

Legacy-message fallbacks — and *only* legacy messages reach them:

| Absent field | Fallback | Why |
|---|---|---|
| `campaign_ids` | job-level `campaign_ids` | all the old message ever carried |
| `match_level` | `exact` | the old consumer's behaviour |
| `start_char` | `0` | the old consumer's behaviour |
| `end_char` | `start_char + len(matched_text)` | the old consumer's behaviour |
| `confidence` | `1.0` | the old consumer's behaviour |

### Size discipline

SQS allows **1 MiB** (verified). The application enforces
`MAX_MESSAGE_BYTES = 65 536` — a self-imposed ceiling ~16× below the service
limit — so oversized payloads fail in our validator with a named error instead of
at the AWS boundary. Transcripts travel **by reference** (`transcript_id`), never
inline. Audio bytes never appear in a message. `matched_keywords` is capped at 50
entries and `campaign_ids` at 200; overflow is truncated with a recorded
`truncated: true` flag rather than silently dropped.

### Visibility

Base visibility is `RADIO_SQS_VISIBILITY_SECONDS` (default 300, validated
30..43 200 — the documented 12-hour maximum). A background heartbeat extends
visibility every `RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS` while ASR or LLM work
is in progress, up to `RADIO_SQS_MAX_PROCESSING_SECONDS`. Extension is not
optional: an expired visibility timeout on a slow segment makes the *next*
segment of that station visible early and breaks the ordering guarantee that
motivated the group choice.

### Dead-letter handling

Redrive is a queue attribute (`RedrivePolicy` / `maxReceiveCount`) configured by
infrastructure. The application **never** sends to a DLQ itself. Permanent
failures are recorded in `processing_failures` and the message is then deleted,
so a message we already understand does not burn `maxReceiveCount` attempts.

### Local queue backend

`RADIO_QUEUE_BACKEND ∈ {sqs, memory}`. `memory` is an in-process implementation
with the same interface and the same FIFO group semantics, used by the test suite
and `compose.dev.yaml`. It exists so the contract tests exercise real ordering
and dedup logic without AWS.

## Alternatives considered

1. **Standard queues + application ordering.** Rejected: at-least-once with no
   ordering means reconstructing sequence in the assembler and building our own
   dedup window; FIFO gives both for free.
2. **`MessageGroupId = f"{station_id}:{sequence % K}"`** to parallelise within a
   station. Rejected *for now*: breaks per-station ordering, which the
   conversation assembler depends on. Revisit only if (a) the assembler is made
   fully order-independent with a reorder buffer, and (b) queue age proves the
   per-group ceiling is the binding constraint.
3. **`MessageGroupId = conversation_id` for analysis.** Rejected: no throughput
   gain against a single shared LLM, and it allows out-of-order per-station
   results.
4. **EventBridge / SNS / Kinesis.** Rejected: no per-group FIFO with a
   consumer-driven visibility model; more moving parts for the same guarantee.
5. **Inline transcripts in analysis messages.** Rejected: transcripts routinely
   exceed the self-imposed ceiling and would put broadcast content into queue
   storage.

## Consequences

* One in-flight segment per station at a time — a real ceiling, documented and
  measured rather than discovered in production.
* Adding workers scales *across* stations, not within one.
* Schema versioning makes a v2 additive: consumers accept `{v1, v2}` during a
  rollout window.
* The `memory` backend keeps the contract tests hermetic and fast.

## Operational risks

| Risk | Mitigation |
|---|---|
| One slow station blocks its own backlog | Per-group isolation means other stations are unaffected; `transcription_queue_age_seconds` alerts; segments older than the ring window are dropped by policy rather than queued forever |
| Visibility expiry mid-ASR → duplicate delivery | Heartbeat extension + inbox dedup makes duplicates a no-op |
| Dedup interval (5 min) shorter than a retry gap | `segment_id`/`analysis_job_id` are stable and the inbox is the real dedup authority; SQS dedup is defence in depth, not the guarantee |
| Poison message loops | Permanent-error classification records and deletes |
| Clock skew in `created_at` | Ordering comes from the FIFO group, never from timestamps |

## Security impact

* No credentials, presigned URLs, audio bytes or transcript bodies in messages.
* Strict Pydantic validation on every field: UUID format, ISO-8601 timestamps,
  station-id pattern, enum membership, and a hard field-length cap.
* At INFO level only ids are logged; full bodies only at DEBUG, which is off by
  default in production.
* `station_id` is validated against the same pattern used for systemd unit
  interpolation in the legacy reconciler, so a hostile queue message cannot smuggle
  a shell- or path-significant identifier into a downstream component.

## Cost impact

FIFO request pricing. At 8 stations × 3 segments/minute: ≈ 1 440 messages/hour
transcription and far fewer analysis messages, each costing one send + one
receive + one delete (plus visibility extensions on long jobs). Long polling
(20 s, the documented maximum) minimises empty receives.

## Test requirements

* Valid v1 messages round-trip; unknown `schema` rejected as permanent.
* Oversized body rejected by our validator before any SQS call.
* Dedup id and group id are exactly `segment_id`/`station_id` and `analysis_job_id`/`station_id`.
* Duplicate delivery of an already-processed message is a no-op with no second
  business row.
* Visibility extension is called for a job that outlives the base timeout.
* Retryable vs permanent classification: message left visible vs recorded+deleted.
* Invalid UUID, invalid timestamp, unknown storage backend, negative
  `duration_ms`, and an over-long `station_id` are all rejected.
* FIFO group ordering is preserved by the `memory` backend (contract parity test).

## Reversal strategy

`RADIO_QUEUE_BACKEND=memory` degrades to single-process operation for
diagnostics. Reverting the whole hand-off is `RADIO_PIPELINE_MODE=legacy`
(ADR-001). Draining is the normal path: stop producers, let consumers finish, then
switch — messages are self-describing and no consumer depends on producer state.
