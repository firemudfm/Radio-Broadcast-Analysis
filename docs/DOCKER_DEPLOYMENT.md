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
RADIO_UID="$(id -u radio)"; RADIO_GID="$(id -g radio)"

sudo install -d -m 0700 -o "$RADIO_UID" -g "$RADIO_GID" /var/lib/radio/spool
sudo install -d -m 0750 -o "$RADIO_UID" -g "$RADIO_GID" \
    /var/lib/radio/database /var/lib/radio/evidence \
    /var/lib/radio/logs /var/lib/radio/backups
sudo install -d -m 0755 -o "$RADIO_UID" -g "$RADIO_GID" /var/lib/radio/models
sudo install -d -m 0750 -o "$RADIO_UID" -g "$RADIO_GID"     /var/lib/radio/releases /var/lib/radio/deploy
```

**The container uid/gid is a build argument, not a constant.** `10001` is only
the default for local development; this host's `radio` account is **992**, and
the images are built with that so bind mounts are writable without a recursive
`chown` of the data volume.

Set it in `compose.env`:

```
RADIO_CONTAINER_UID=992
RADIO_CONTAINER_GID=992
```

`scripts/deploy-compose.sh` reads `id -u radio` / `id -g radio` automatically
and warns if `compose.env` disagrees. It **verifies** ownership and refuses to
deploy when it is wrong, printing the exact `chown` to run — it never applies
one itself, because a recursive `chown` across a spool full of evidence during
a deploy is not something a script should decide to do.

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
sudo -u radio python3 scripts/download-models.py --root /var/lib/radio/models
sudo -u radio python3 scripts/verify-models.py  --root /var/lib/radio/models
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

## 6a. Deploying a release (exact-commit)

`scripts/deploy-compose.sh` deploys **one reviewed commit**, never a branch.

```bash
sudo scripts/deploy-compose.sh --commit <40-hex-sha> --stage api
```

### Why exact commits

A branch name would let the deployed content change between approval and
execution. The script refuses anything that is not a full 40-character sha —
short shas, tags and branch names included — and it **never runs `git pull`,
`git fetch`, `git reset` or `git checkout`**. Whoever approves the commit is
responsible for it being present locally; the deployment step itself is offline
and auditable.

### Release layout

```
/var/lib/radio/releases/
    <full-git-sha>/            immutable, created with `git archive`
    current  -> <full-git-sha>
    previous -> <full-git-sha>

/var/lib/radio/deploy/
    state.json                 non-secret deployment state, written atomically
    history/                   one snapshot per deployment
    logs/

/var/lock/radio-compose-deploy.lock
```

A release contains no `.git`, no env file, no model, no database and no audio.
`git archive` cannot include them, and the script asserts their absence anyway.

### Stages

| Stage | Starts | Needs models |
|---|---|---|
| `api` (default) | api | no |
| `core` | api, planner | no |
| `full` | api, planner, listener, transcription, analysis, cleanup, llm | **yes** |

`full` is never the default: it starts live capture. It verifies models with
`scripts/verify-models.py` before starting anything and refuses if they are
absent — it never downloads them.

Deploy `api` first on a new host, confirm `/readyz`, then widen.

### The deployment lock

An exclusive `flock` on `/var/lock/radio-compose-deploy.lock` serialises
deployments and rollbacks. Two concurrent runs would race on the release
symlinks and the database backup. The lock is released on exit, including on
failure.

### Order of operations

1. Every validation gate (commit, clean source, mount, ownership, env files,
   permissions, placeholder secret, static credentials, exposure policy, disk).
2. Immutable release via `git archive`, then a secret scan inside it.
3. `docker compose config` from the release.
4. Build images tagged with the exact sha.
5. **SQLite backup** (`sqlite3 .backup`, verified) if a database exists.
6. **Migration** in a one-shot container with `--network none`.
7. Start services, wait for container health.
8. Run `scripts/smoke-test.sh` against loopback.
9. Only then move `current` / `previous` and write `state.json`.

### Failure semantics

The distinction matters and the script reports it explicitly:

* **Failure before any container changed** — the running release is completely
  untouched. Only the failed temporary release directory is cleaned up. This is
  *not* a rollback, and the script does not call it one.
* **Failure after containers began changing** — the database is **not**
  restored, the new backup is preserved, logs are kept, and the exact failed
  health check is reported with the rollback command to run.

`current` and `previous` never move until the new release is healthy.

---

## 6b. Rolling back

```bash
sudo scripts/rollback-compose.sh --previous
sudo scripts/rollback-compose.sh --to-commit <40-hex-sha>
sudo scripts/rollback-compose.sh --previous --dry-run
```

**Code and images roll back. The database does not.**

That is deliberate. Restoring the SQLite file would discard every mention,
transcript and analysis written since the backup, and the schema is
forward-only by policy (ADR-004). A backup *is* taken before the rollback, so
if older code turns out to be incompatible with the newer schema you can
restore it — as a separate, explicit operator action, never as a side effect.

Rollback never rebuilds: the target commit's images must still exist locally.

---

## 6c. Database migration on its own

```bash
sudo scripts/migrate-db.sh --image radio-api:<sha>
sudo scripts/migrate-db.sh --image radio-api:<sha> --check-only
```

Runs `python -m app.cli.migrate_database` in a one-shot container with
`--network none`, no published port and only the database and log directories
mounted. It reuses the same `Database` class and `run_migrations` as normal
start-up — a different entrypoint, not a second migration engine.

`--database PATH` is a real escape hatch: given an explicit path, the CLI runs
without the rest of the application configuration and falls back to the SQLite
defaults, printing `configuration unavailable ... continuing`. Recovering a
schema should not require a bucket name and an audio-token secret to be valid
first. Without `--database` there is no way to know which file to migrate, so a
broken configuration is still a usage error (exit 64).

---

## 6d. Spool cleanup on demand

```bash
sudo scripts/cleanup-spool.sh --image radio-pipeline:<sha> --dry-run
sudo scripts/cleanup-spool.sh --image radio-pipeline:<sha>
```

A thin wrapper with no deletion policy of its own. Retention, watermarks and
containment are enforced by `app/workers/cleanup.py`, which joins every
candidate against SQLite job state. A `pending` segment or audio belonging to a
confirmed mention is never deleted, at any watermark.

---

## 6e. Direct HTTP exposure (restricted pilot only)

The API binds `127.0.0.1:8788` by default. To publish it directly, **both** are
required in `compose.env`:

```
RADIO_API_PUBLISH_HOST=0.0.0.0
RADIO_ALLOW_DIRECT_HTTP=1
```

Without the acknowledgement the deployment fails before anything is built.

This is a **restricted-pilot option, not the recommended architecture.** The
API runs with `auth_mode=none` and no TLS; nothing in this repository can
restrict who reaches it, so that must be enforced by the host firewall or
security group. A reverse proxy with TLS remains deferred work and is not part
of this repository.

---

## 6f. Deployment state

`/var/lib/radio/deploy/state.json`, written through a temporary file and
renamed atomically:

```json
{
  "schema_version": 1,
  "current_commit": "…", "previous_commit": "…",
  "deployed_at": "…", "deployed_by": "…", "stage": "api",
  "compose_project": "radio-prod", "release_path": "…",
  "api_image": "radio-api:…", "api_image_id": "sha256:…",
  "migration": "ok", "backup_path": "…", "smoke_test": "pass"
}
```

It holds no credential, no token, no environment-file content and no transcript.
Logs are under `/var/lib/radio/deploy/logs/`.

### Not yet automated

There is deliberately **no** GitHub production deployment workflow and **no**
deployment SSM document in this repository. The GitHub OIDC role remains
smoke-only. These scripts are the reviewed foundation a fixed SSM document can
call later; `AWS-RunShellScript` is not an acceptable transport for it.

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
| Container cannot write the spool | Host dir not owned by the container uid | `ls -ln /var/lib/radio`, compare with `id -u radio` |
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
