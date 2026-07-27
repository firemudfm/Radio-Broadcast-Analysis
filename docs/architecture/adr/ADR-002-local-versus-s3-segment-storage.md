# ADR-002 — Local spool first; S3 segment storage for distributed mode

Status: **Accepted** · Date: 2026-07-27

## Context

The pipeline produces a 20–30 s speech segment roughly every 20–30 s per active
station. Most of those segments match no keyword and are deleted within minutes.

At 8 stations that is ≈ 1 000 segments/hour, ≈ 24 000/day. Round-tripping every
one of them through S3 on a single-node deployment means 24 000 PUTs + 24 000
GETs + 24 000 DELETEs per day to move ~60 kB of data between two processes on
the same machine — paying latency, request cost and a network dependency for a
localhost hop.

## Decision

Define a `SegmentStore` abstraction with two implementations, selected by
`RADIO_SEGMENT_STORE`:

| Backend | Default | Purpose |
|---|---|---|
| `LocalSegmentStore` | **yes** | Single-node: write under `/var/lib/radio/spool`, share via a bind mount |
| `S3SegmentStore` | no | Multi-host workers, cross-host handoff, recovery, explicit overflow, troubleshooting, distributed benchmarks |

Every queue message carries a self-describing storage descriptor, so a consumer
never infers the backend from configuration:

```json
{"backend": "local", "path": "/var/lib/radio/spool/<station>/<segment>.opus",
 "bucket": null, "key": null, "sha256": "…", "size_bytes": 123456}
```

```json
{"backend": "s3", "path": null, "bucket": "…",
 "key": "temp-speech/<station>/<segment>.opus", "sha256": "…", "size_bytes": 123456}
```

Writes are atomic in both backends: local writes go to `<name>.tmp`, are
`fsync`-ed, then `os.replace`-d into place (rename within a filesystem is
atomic); S3 PUTs are single-object and idempotent by deterministic key.

### Path safety rules (enforced, not documented-only)

1. The configured spool root is resolved once with `Path.resolve(strict=False)`.
2. Every candidate path is resolved and must satisfy
   `resolved.is_relative_to(root)`. Because `resolve()` follows symlinks, this
   rejects both `../` traversal and symlink escape in one check.
3. `station_id` and `segment_id` are additionally validated against strict
   patterns before ever touching the filesystem; `..`, `/`, `\`, NUL and
   absolute paths are rejected at the identifier layer.
4. On read, `O_NOFOLLOW` is used for the final open where the platform supports
   it, and the opened file's `st_dev`/`st_ino` are re-checked against the
   `lstat` performed during validation (TOCTOU defence).
5. SHA-256 is verified before the bytes are handed to the decoder.

## Alternatives considered

1. **S3 for everything.** Rejected: cost and latency for data whose median
   lifetime is minutes, plus a hard network dependency in the hot path of a
   single-node deployment.
2. **Local only.** Rejected: forecloses multi-host workers, which is the entire
   scaling story in ADR-008.
3. **Shared EFS/NFS.** Rejected: another managed dependency, worse
   consistency/latency than S3 for this access pattern, and no benefit over a
   bind mount on one node.
4. **Audio bytes inside the SQS message.** Rejected outright. A 20 s Opus
   segment is ~60 kB and would fit inside the 1 MiB limit, but it would put
   audio in queue storage and in CloudTrail-adjacent surfaces, remove the
   checksum-before-decode step, and make redelivery expensive. Never do this.

## Consequences

* Single-node is fast and cheap; the localhost hop is a `rename` plus a bind mount.
* `listener`, `transcription-worker`, `analysis-worker` and `cleanup-worker`
  must share the spool mount in local mode. Compose enforces this.
* Disk becomes a first-class resource with watermarks (see ADR-010 / §28).
* Distributed mode requires no code change — only `RADIO_SEGMENT_STORE=s3` and
  a bucket, because the descriptor already travels with the message.

## Operational risks

| Risk | Mitigation |
|---|---|
| Spool fills the root filesystem | Watermarks at 70/85/90 %; emergency reclaim deletes only expired, non-in-flight, non-confirmed segments; `docs/OPERATIONS.md` requires a dedicated volume |
| Orphaned files after a crash | `cleanup-worker` cross-checks `audio_segments` job state before deleting — never age alone |
| Consumer on another host with `backend=local` | The consumer verifies the descriptor's backend against its own configuration and raises a *permanent* `SegmentUnavailable`, recorded in `processing_failures` rather than retried forever |
| Stale `.tmp` files | Cleanup removes `*.tmp` older than one hour |

## Security impact

Positive relative to the alternatives.

* No presigned URL is ever generated for segments, so none can leak into a log.
* No AWS credential appears in any message.
* Path traversal and symlink escape are structurally prevented, with tests.
* Spool files are written `0640`, owned by the container's non-root user.
* Checksum verification means a tampered spool file fails closed rather than
  being transcribed.

## Cost impact

Local mode: **zero** S3 requests for temporary audio. S3 mode at 8 stations:
≈ 24 000 PUT + 24 000 GET + 24 000 DELETE/day, plus short-lived storage. The
default therefore avoids essentially all of the temporary-audio S3 cost.

## Test requirements

* `../` traversal rejected (relative, absolute, encoded).
* Symlink pointing outside the root rejected on both write and read.
* Checksum mismatch → `ChecksumMismatch` (permanent), segment quarantined.
* Missing segment → `SegmentMissing` (permanent, not retried forever).
* Atomic write: no partially written file is ever visible under the final name.
* Concurrent cleanup does not delete a segment referenced by a `running` job.
* Backend mismatch between descriptor and configuration is a permanent error.
* Round-trip equality for both backends against the same fixture.

## Reversal strategy

Flip `RADIO_SEGMENT_STORE`. Both stores are always constructible; the setting
picks the writer. In-flight messages carry their own descriptor, so a mode change
does not strand them — messages written before the flip continue to resolve
against the backend named in their own payload.
