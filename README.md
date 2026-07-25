# Radio Broadcast Analysis

A self-hosted radio intelligence backend. It records live radio stations, filters speech from music, transcribes it with a multilingual model, matches campaign keywords (exact and semantic, across languages), scores sentiment, and serves everything through a FastAPI service that a web dashboard consumes

Everything runs on one EC2 instance with a local LLM. No external AI APIs are called.

## What it does

1. **Record**: an ffmpeg capture unit per station writes 5-minute audio chunks.
2. **Upload**: chunks and manifests go to S3 (`raw-audio/<station>/YYYY/MM/DD/`).
3. **Filter + transcribe**: a worker keeps only speech segments (Silero + YAMNet) and transcribes them with multilingual faster-whisper.
4. **Match**: keywords from active campaigns are matched against transcripts. Exact matching is case and accent insensitive with per-language aliases. Keywords typed `topic`/`concept` also get semantic scans by the local LLM, which accepts translated and related-topic passages.
5. **Analyze**: a shared worker assembles the full conversation session around each mention (real speech-gap boundaries, not a fixed window) and asks the local LLM (Qwen3 0.6B via llama.cpp) for a summary, relevance, key points, sentiment, and confidence.
6. **Serve**: the FastAPI app exposes campaigns, mentions (full transcript with highlight spans), protected audio playback, a worldwide station catalogue (Radio Browser + curated overlay), and station monitoring.

## Components

| Component | Runs as | Purpose |
|---|---|---|
| `app/main.py` (FastAPI) | `radio-intelligence-api.service` | HTTP API on port 8788 |
| `app/station_reconciler.py` | `radio-station-reconciler.service` (root) | The only process that touches systemd; probes, starts, and stops station pipelines |
| Analysis worker | `radio-analysis-worker.service` | Full-transcript LLM analysis and semantic discovery |
| Local LLM | `radio-llm.service` | Qwen3 0.6B on `127.0.0.1:8790`, idle sleep after 10 minutes |
| Per-station ingestion | `radio-capture@<id>`, `radio-uploader@<id>`, `radio-pipeline-worker@<id>` | Record, upload, filter/transcribe/match |

State lives in SQLite (`radio.db`). Audio, transcripts, and results live in S3. One shared LLM and one shared analysis worker serve all campaigns; nothing is spawned per campaign.

## Station lifecycle

Stations run only while an active campaign references them.

- Campaign created or resumed: its stations are probed (SSRF-safe, ffprobe) and started, up to `RADIO_MAX_ACTIVE_STATIONS`. Extra stations wait in `pending_capacity` and are promoted automatically when a slot frees.
- Campaign paused or deleted: unreferenced stations stop after `RADIO_STATION_STOP_GRACE_SECONDS` (default 300).
- A campaign resumed after its stations were wound down gets them revived and restarted automatically.
- Manual activation without a campaign runs only for the grace period; the API response says so explicitly.

## Requirements

- Python 3.11+
- ffmpeg / ffprobe (unit tests mock them; needed at runtime)
- An S3 bucket the instance role can read and write
- For deployment: Amazon Linux 2023 with systemd

## Local development

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Run the test suite (no AWS, no network, subprocesses mocked)
venv/bin/python -m pytest tests/ -q
```

Run the API locally:

```bash
export RADIO_S3_BUCKET=<YOUR_S3_BUCKET>
export RADIO_AUDIO_TOKEN_SECRET=$(openssl rand -hex 32)
export RADIO_DATABASE_PATH=./radio.db
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8788
```

Check it: `curl localhost:8788/healthz`, interactive docs at `http://localhost:8788/docs`.

## Key configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `RADIO_S3_BUCKET` | required | Bucket for audio, transcripts, results |
| `RADIO_AUDIO_TOKEN_SECRET` | required | HMAC secret for audio/preview tokens (32+ chars) |
| `RADIO_DATABASE_PATH` | `/var/lib/firemud/...` | SQLite path |
| `RADIO_MAX_ACTIVE_STATIONS` | 2 | Concurrent station pipelines |
| `RADIO_STATION_STOP_GRACE_SECONDS` | 300 | Wind-down delay for unreferenced stations |
| `RADIO_MENTION_WINDOW_DAYS` | 7 | Dashboard sentiment and count window (1 = last 24 hours) |
| `RADIO_MENTION_AUDIO_PAD_SECONDS` | 2.0 | Playback padding around a keyword; large values play the whole speech segment |
| `RADIO_SEMANTIC_DISCOVERY_ENABLED` | true | LLM cross-language keyword scans |
| `RADIO_LLM_BASE_URL` | `http://127.0.0.1:8790` | Local LLM endpoint |
| `RADIO_API_CORS_ORIGINS` | localhost dev ports | Allowed browser origins |
| `RADIO_LEGACY_PINNED_STATION_IDS` | empty | Stations the reconciler must never stop |

See `app/config.py` for the full list with validation ranges.

## API overview

- `GET /healthz` health and component status
- `GET /api/v1/brand-signal/dashboard` campaigns, recent mentions, sentiment summary
- `POST /api/v1/brand-signal/campaigns` create a campaign (`station_ids` or `station_selection` with `explicit` / `country_top` / `country_all` modes)
- `GET /api/v1/brand-signal/mentions/{id}/detail` full conversation transcript, highlight spans, LLM analysis
- `POST /api/v1/brand-signal/mentions/{id}/audio-token` then `GET /api/v1/brand-signal/audio/{token}` protected audio playback
- `GET /api/v1/radio-catalog/stations` worldwide station search (countries, languages, tags, health)
- `POST /api/v1/radio-catalog/stations/{uuid}/preview-token` short-lived stream preview through the server (the browser never sees stream URLs)
- `GET /api/v1/monitoring/capacity`, `POST /api/v1/monitoring/stations/{uuid}/activate`, `.../stop` station monitoring

## Keyword matching policy

- Exact matching: casefolded, diacritics stripped, whole-word tokens, per-language aliases (`Value` plus aliases like `Regierung`, `governo`).
- `topic` / `concept` keywords: semantic matching defaults on; the LLM may accept translated equivalents and related-topic passages, but only when it can return a verbatim on-air phrase to highlight and timestamp.
- `brand` / `person` / `product` / `organization`: semantic matching defaults off; when enabled, translated brand names and broad concepts are still rejected.
- Every semantic decision is audited under `results/semantic-matches/` in S3.

## Deployment to EC2

`EC2_UPGRADE.md` documents the full procedure. Short version:

```bash
# On the instance
sudo ./deploy/upgrade-to-v0.4.0-amazon-linux.sh   # installs venv, app, systemd units
./deploy/audit-v040.sh                            # post-install checks
curl -s localhost:8788/healthz
```

Configuration lives in `/etc/firemud/radio-intelligence.env`. Restart after changes:

```bash
sudo systemctl restart radio-intelligence-api radio-station-reconciler
```

Watch live logs:

```bash
sudo journalctl -f -u 'radio-*'
```

## CI/CD

Five connected pipelines under `.github/workflows/`:

```text
push / PR
   |
   v
  CI  (ci.yml) ......... lint (ruff) + tests on Python 3.11 and 3.12
   |                     + security gates (bandit medium+, pip-audit CVEs)
   | success on main
   v
  CD  (deploy.yml) ..... preflight (reachable, disk, services healthy)
   |                     -> deploy the exact CI-tested commit (rsync + ci-deploy.sh:
   |                        compile check BEFORE swap, dep install, restart, health check)
   |                     -> live smoke tests against the public API
   |                     -> rollback job if deploy or smoke tests fail
   |
  Health monitor (health.yml) ... every 6 hours: services, disk, memory,
   |                              journal errors, public /healthz
  Rollback (rollback.yml) ....... one-click manual restore of the previous release
  CodeQL (codeql.yml) ........... weekly + per-push deep security analysis
```

Deploy safety, in order: CI must be green, preflight must pass, the new code must compile on the instance before the old code is touched, a timestamped backup is kept under `/var/backups` (five most recent), the services must come back active, `/healthz` must answer, and the CD run then smoke-tests the public API. Any failure rolls the code back automatically.

Per-station capture/uploader/worker units are not restarted by deploys; they run the separate ingestion package and keep recording through an API deploy.

One-time setup (repo Settings, then Secrets and variables, then Actions):

| Secret | Value |
|---|---|
| `EC2_HOST` | The instance public IP or DNS name |
| `EC2_SSH_KEY` | The full contents of the instance's private key file (PEM) |

Notes:

- Without an Elastic IP, the instance IP changes on every stop/start and `EC2_HOST` must be updated to match. Allocating an Elastic IP removes that chore.
- The instance security group must allow SSH (port 22) from GitHub-hosted runners for deploys and the health monitor to connect.
- Every workflow can also be run manually from the Actions tab.

## Security notes

- This is an open pilot: `auth_mode=none`, no sign-in. Restrict the API port (8788) and SSH (22) to trusted IPs in your security group before exposing the instance anywhere.
- The browser never receives stream URLs, S3 URIs, or credentials. Previews and mention audio go through short-lived HMAC tokens.
- All outbound stream fetches are SSRF-guarded (`app/services/net_safety.py`): public IPs only, redirects re-validated on every hop.
- The reconciler is the only root process; the API runs unprivileged and can only queue jobs in SQLite.

## Tests

```bash
venv/bin/python -m pytest tests/ -q
```

140+ tests cover the catalogue, the monitoring lifecycle (capacity, promotion, wind-down, revive after resume), campaigns, keyword matching, semantic discovery, sync, audio byte ranges, SSRF guards, and the reconciler against mocked systemd/ffprobe.

`VALIDATION.md` records what was verified at each release, including field bugs found on real hardware and their regression tests. `RELEASE_NOTES.md` has the detailed v0.3/v0.4 release write-up.
