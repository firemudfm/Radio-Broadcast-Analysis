# Docker deployment

How to run the shared-station pipeline under Docker Compose on the ARM64 EC2
target, and how to get back to the legacy deployment if it does not work out.

The legacy systemd deployment is unchanged and still supported. Nothing in this
document is required to keep the v0.4 system running.

---

## 1. What gets deployed

| Service | Image | Replicas | Publishes | Purpose |
|---|---|---|---|---|
| `api` | `radio-api` | 1 | `127.0.0.1:8788` | FastAPI control plane |
| `planner` | `radio-pipeline` | 1 | – | Station reconciliation, keyword indexes, outbox dispatch |
| `listener` | `radio-pipeline` | 1 per shard | – | Shared station sessions, segment capture |
| `transcription-worker` | `radio-pipeline` | 1+ | – | ASR, keyword matching, conversation assembly |
| `analysis-worker` | `radio-pipeline` | 1+ | – | Local LLM analysis, result persistence |
| `cleanup-worker` | `radio-pipeline` | 1 | – | Spool retention and back-pressure |
| `llm` | `radio-llm` | 1 | **nothing** | `llama-server` on the Compose network only |

`planner`, `listener` and `cleanup-worker` are **singletons**. Two planners
would both dispatch the outbox (safe, but duplicated); two listeners on the
same shard index would both connect to the same stations, which defeats the
entire point of the design. Scale listeners by adding *shards*, not replicas —
see §7.

Only `api` publishes a port, and in production only on loopback. Put a reverse
proxy in front of it for TLS.

Port 8790 is deliberately **not** published. An unauthenticated inference
endpoint reachable from the host — let alone the internet — is a serious
liability, and `scripts/compose-check.sh` fails if anything publishes it.

---

## 2. Prerequisites

* Docker Engine 24+ and Compose v2.24+ (the `!override` tag in the overlays
  needs 2.24).
* ~4 GB free for images, plus spool and model space (§4).
* An EC2 instance role granting S3 and SQS access. Do **not** put static AWS
  keys in an env file: they have no rotation and no audit trail.

Verify the toolchain before anything else:

```bash
docker --version
docker compose version
```

---

## 3. Host layout

```
/etc/radio-broadcast-analysis/
    infrastructure.env      # account-specific: region, bucket, queue URLs
    application.env         # behaviour and secrets

/var/lib/radio/
    database/               # SQLite + WAL           (api, all workers: rw)
    spool/                  # transient audio        (listener, workers: rw)
    models/                 # ASR + LLM weights      (workers: ro)
    evidence/               # retained clips         (analysis, cleanup: rw)
    logs/
    backups/
```

```bash
sudo install -d -m 0750 /etc/radio-broadcast-analysis
sudo install -d -m 0755 /var/lib/radio
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/radio/spool
sudo install -d -m 0750 -o 10001 -g 10001 \
    /var/lib/radio/database /var/lib/radio/evidence \
    /var/lib/radio/logs /var/lib/radio/backups
sudo install -d -m 0755 -o 10001 -g 10001 /var/lib/radio/models
```

uid/gid **10001** is fixed in all three Dockerfiles. Host directories must be
owned by it, otherwise a non-root container cannot write to a bind mount and
the failure surfaces as a confusing permission error at first segment write
rather than at start-up.

`spool/` is `0700` because it holds raw broadcast audio.

---

## 4. Configuration

Start from `.env.example`, which documents every setting and its default:

```bash
sudo cp .env.example /etc/radio-broadcast-analysis/application.env
sudo chmod 0640 /etc/radio-broadcast-analysis/application.env
sudo chown root:root /etc/radio-broadcast-analysis/application.env
```

Split it: account-specific values (region, bucket, queue URLs) into
`infrastructure.env`, everything else into `application.env`. Compose reads
both.

Generate the audio-token secret — the file must never contain the placeholder:

```bash
python3 -c "import secrets; print('RADIO_AUDIO_TOKEN_SECRET=' + secrets.token_urlsafe(48))"
```

The settings that actually decide behaviour:

```bash
RADIO_PIPELINE_MODE=shared_sqs        # `legacy` is the default; opt in explicitly
RADIO_QUEUE_BACKEND=sqs
RADIO_SEGMENT_STORE=local             # see ADR-002
RADIO_TRANSCRIPTION_QUEUE_URL=https://sqs.<region>.amazonaws.com/<acct>/<name>.fifo
RADIO_ANALYSIS_QUEUE_URL=https://sqs.<region>.amazonaws.com/<acct>/<name>.fifo
RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8    # UNIQUE ACTIVE stations. Not campaigns.
```

Both queue URLs must end in `.fifo`. The application refuses to start
otherwise: per-station ordering is a correctness requirement, and a standard
queue would silently reorder segments within a station.

Validate the configuration *before* deploying:

```bash
make compose-check
```

This resolves the full Compose configuration and asserts the security posture
against the resolved output — reading the source files is not equivalent,
because an overlay can reintroduce something the base forbade.

---

## 5. Models

Models are never baked into an image and never downloaded at container
start-up. See [MODEL_MANAGEMENT.md](MODEL_MANAGEMENT.md).

```bash
sudo -u '#10001' python3 scripts/download-models.py --root /var/lib/radio/models
sudo -u '#10001' python3 scripts/verify-models.py  --root /var/lib/radio/models
```

Roughly 1.1 GB. Do this before the first `up`, or the transcription worker
will fail with a named `model_verification_failed` error and the LLM container
will exit 78.

---

## 6. Deploying

Build for this host's architecture:

```bash
make build
```

Start the control plane first and confirm it is healthy, then the pipeline:

```bash
sudo RADIO_ENV_DIR=/etc/radio-broadcast-analysis \
  docker compose -f compose.yaml -f compose.prod.yaml --profile core up -d

curl -fsS http://127.0.0.1:8788/readyz | python3 -m json.tool

sudo RADIO_ENV_DIR=/etc/radio-broadcast-analysis \
  docker compose -f compose.yaml -f compose.prod.yaml \
  --profile core --profile pipeline --profile llm up -d
```

Staging the start is not ceremony: the workers `depend_on` the API being
healthy because the API owns schema migration, and a worker that races an
unmigrated database fails in a much less obvious way.

Then:

```bash
scripts/smoke-test.sh http://127.0.0.1:8788
```

Expected on a correct deployment: `/healthz` ok, `/readyz` ready, all four
components `ok`, spool `ok`, `auth_mode: none`.

---

## 7. Scaling

**Vertically, first.** Raise `RADIO_MAX_ACTIVE_UNIQUE_STATIONS` and
`RADIO_LISTENER_MAX_SESSIONS` together, then watch `docker stats` and
`queue_age_seconds`. If the queue age grows without bound, the transcription
worker is the bottleneck, not the listener.

**Horizontally**, add listener shards. Sharding is deterministic
(BLAKE2b over the station id, never Python's randomised `hash()`), so two
containers divide stations with no coordination:

```yaml
listener-0:
  environment:
    RADIO_LISTENER_SHARD_COUNT: "2"
    RADIO_LISTENER_SHARD_INDEX: "0"
listener-1:
  environment:
    RADIO_LISTENER_SHARD_COUNT: "2"
    RADIO_LISTENER_SHARD_INDEX: "1"
```

Every listener must agree on `SHARD_COUNT`. A mismatch means some stations are
covered twice and others not at all — and neither shows up as an error.

Transcription and analysis workers scale by plain replica count; SQS
distributes work and the inbox makes redelivery safe.

Moving workers to a second host requires `RADIO_SEGMENT_STORE=s3`, because the
local spool is not shared. See ADR-002.

---

## 8. Rolling back

The fastest rollback is a mode flip, not a redeploy:

```bash
sudo sed -i 's/^RADIO_PIPELINE_MODE=.*/RADIO_PIPELINE_MODE=legacy/' \
    /etc/radio-broadcast-analysis/application.env

sudo docker compose -f compose.yaml -f compose.prod.yaml \
    --profile pipeline --profile llm down

sudo systemctl start radio-intelligence-api
```

The pipeline workers refuse to start in `legacy` mode, so this is
self-enforcing. No schema change is needed in either direction: migrations run
in **both** modes deliberately, so switching is a configuration change rather
than a data migration.

The v0.4 tables and routes are untouched by the new path, so legacy data
remains readable throughout.

---

## 9. Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Worker exits immediately with `RADIO_PIPELINE_MODE is 'legacy'` | Mode not enabled | `application.env` |
| Container cannot write the spool | Host dir not owned by 10001 | `ls -ln /var/lib/radio` |
| `model_verification_failed` | Models absent or truncated | `scripts/verify-models.py` |
| LLM container exits 78 | GGUF missing | `ls -l /var/lib/radio/models/qwen/` |
| `/readyz` 503, components `absent` | Workers not started | `docker compose ps` |
| `queue_age_seconds` climbing | Consumers behind | Add a transcription worker |
| Spool at `pause` | Cleanup behind or disk small | `docker logs ...cleanup-worker` |
| Segments produced, no mentions | Keyword index empty for the station | `GET /api/v1/monitoring/pipeline` |

Logs are JSON in production. Every job carries `trace_id`, `station_id`,
`segment_id`, `conversation_id` and `mention_id`, so one mention can be traced
end to end:

```bash
docker compose logs --no-log-prefix transcription-worker \
  | grep '"trace_id":"<id>"' | python3 -m json.tool
```

Transcript bodies are **not** logged at INFO. They are the product's output,
not operational data; `RADIO_LOG_TRANSCRIPT_BODIES=true` changes that and
should stay off outside debugging.
