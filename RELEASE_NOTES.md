# FireMud Radio Backend v0.4.0 — Full Transcript + Local Multilingual LLM

## v0.4.0: capacity-aware multi-radio catalogue and monitoring

New on top of the v0.3.1 conversation backend:

- `/api/v1/radio-catalog/*`: countries, languages, tags, codecs, paginated
  station search, and station detail from the Radio Browser distributed API,
  merged with the bundled curated override/deletion snapshot (419 overrides,
  354 deletions). Public responses never contain stream URLs.
- `/api/v1/monitoring/*`: capacity, managed stations, selection estimates,
  capacity-aware activation (hard limit RADIO_MAX_ACTIVE_STATIONS=2), stop
  with shared reference counting and a grace period.
- Signed short-lived station previews proxied through FastAPI (60 s cap,
  2 concurrent), SSRF-validated on every redirect hop.
- `radio-station-reconciler.service`: the narrow root daemon that probes
  (ffprobe as the radio user), writes /etc/radio-pipeline configs from the
  installed hertz879 template, and starts/stops per-station systemd units.
  One capture/uploader/worker pipeline per unique active station; campaigns
  share stations. Never a Whisper or LLM worker per campaign.
- Campaigns accept `station_selection` (explicit | country_top | country_all)
  while the legacy `station_ids` payload keeps working. hertz879 is imported
  as a pinned legacy station and is never stopped.

See EC2_UPGRADE.md, VALIDATION.md, and THIRD_PARTY.md.


This package upgrades the **existing Amazon Linux 2023 EC2 radio pilot**. It does not create another EC2 instance and it does not change the local frontend yet.

## What v0.3.0 changes

```text
Live radio
  -> FFmpeg capture
  -> Silero + YAMNet speech/music filtering
  -> local multilingual faster-whisper
  -> automatic filter/transcription worker
  -> shared campaign transcript matcher (exact + opt-in semantic)
  -> FastAPI + SQLite
  -> whole contiguous conversation transcript around the mention
  -> exact character/timestamp highlight for the mention
  -> local Qwen3 0.6B analysis through llama.cpp
  -> private S3 audio playback
```

The API does **not** use a fixed “20 seconds before / 45 seconds after” window. It scans neighboring source-chunk transcript groups, orders all retained speech by broadcast time, and expands backward and forward until a real speech gap marks the conversation boundary. The default boundary is 30 seconds of no retained speech, with a 30-minute safety ceiling. Those values are session-boundary and safety controls, not a display context window. The response returns the entire assembled session, plus exact character offsets and broadcast timestamps for the matching phrase and its sentence so the frontend can highlight the mention inside the whole transcript.

The background LLM analysis waits six minutes before its first pass so the following radio chunk has time to finish. The detail endpoint remains immediate and returns `analysis.status="pending"` while the one shared worker analyzes the session. If later transcripts extend the same session, the transcript hash changes and the cached LLM result is queued for refresh.

If the filter found only one spoken section before the session boundary, the complete conversation may still be short. That reflects the available spoken audio; the API never fabricates missing conversation and does not include dropped music/singing as transcript text.

## Small local LLM

The package installs:

- `Qwen3-0.6B-Q8_0.gguf` (about 639 MB)
- `llama-server`, bound only to `127.0.0.1:8790`
- four CPU threads, one parallel request, 16k context
- systemd memory ceiling of 3 GiB
- automatic model sleep after 10 idle minutes, so the model weights are released when no campaign analysis is using them

One shared LLM service serves all campaigns. No model process is created per campaign. Campaign stop/pause removes the campaign from active matching; when no pending work remains, the shared model can enter idle sleep rather than keeping a separate model alive for each campaign.

The LLM returns structured analysis:

- complete-discussion summary
- why the target is relevant
- speaker intent
- target-specific sentiment
- target relevance (`direct`, `indirect`, `incidental`, `not_relevant`)
- key points
- verbatim evidence
- confidence and review flag

An incidental entity is forced to neutral in the dashboard to reduce sentence-level sentiment leakage.

## Cross-language semantic matching

Exact alias matching remains the primary and safest method for brands, people, products, and organizations.

The API additionally accepts opt-in semantic keyword fields:

```json
{
  "value": "Hello",
  "aliases": ["Hi"],
  "keyword_type": "concept",
  "semantic_matching": true,
  "semantic_threshold": 0.74,
  "match_mode": "tokens"
}
```

For `concept` and `topic`, semantic matching defaults to enabled. This allows the shared local LLM to accept a clear cross-language equivalent such as `Hello` -> `Hallo`, but only when it can return an exact verbatim on-air phrase to highlight and timestamp.

For `brand`, `person`, `product`, and `organization`, semantic matching defaults to disabled. When explicitly enabled, the verifier accepts only the same named entity, a phonetic/spelling form, or an explicit alias; it rejects broad related concepts and translated brand names.

Keyword discovery scans every retained speech transcript inside each source chunk so a later presenter break is not missed. Once a mention is found, the detail/LLM path switches to dynamic conversation-session assembly and excludes unrelated speech after a real session gap.

Every semantic decision is audited under:

```text
results/semantic-matches/
```

## Services after installation

Required long-running services:

```text
radio-capture@<station>        lightweight FFmpeg capture
radio-uploader@<station>       S3 uploader
radio-pipeline-worker@<station> automatic filter/transcribe (existing Step 3B or compatible Step 4C)
radio-intelligence-api         FastAPI + SQLite + private audio
radio-llm                      Qwen3 0.6B through llama.cpp
radio-analysis-worker          shared semantic discovery + whole-transcript analysis
```

Old extracted ZIPs and source folders do not use CPU or RAM. They may be removed after acceptance. Step 2A, Step 3A, and the automatic station worker are required. Step 4A/4B only need to remain when an installed Step 4C worker still imports them; otherwise they are optional legacy disk usage and are not consuming CPU while idle.

## New API routes

Existing routes remain compatible. Campaign keyword responses now also expose `keyword_type`, `semantic_matching`, and `semantic_threshold`, so the later frontend can display and edit multilingual matching policy.

New routes are:

```text
GET  /api/v1/brand-signal/runtime
GET  /api/v1/brand-signal/mentions/{mention_id}/detail
POST /api/v1/brand-signal/mentions/{mention_id}/analysis
POST /api/v1/brand-signal/campaigns/{campaign_id}/start
POST /api/v1/brand-signal/campaigns/{campaign_id}/stop
```

`GET .../detail` returns the whole transcript immediately. LLM analysis is asynchronous: when no cached analysis exists, the response reports `analysis.status="pending"` and the single shared worker fills it in. `POST .../analysis` can force an immediate refresh for testing.

`GET .../detail` returns:

```json
{
  "mention": {},
  "full_transcript": "the complete available speech transcript...",
  "highlighted_sentence": "the sentence containing the mention",
  "transcript_segments": [],
  "words": [],
  "highlights": [
    {
      "start_char": 123,
      "end_char": 131,
      "text": "TechSara",
      "keyword": "TechSara",
      "method": "timestamp"
    }
  ],
  "analysis": {
    "status": "ready",
    "model": "qwen3-0.6b-q8",
    "summary": "...",
    "sentiment": "positive",
    "target_relevance": "direct"
  }
}
```

The frontend should later render `full_transcript` as plain text and apply highlighting using the character offsets. It must not use HTML returned by the backend.

## Prerequisites on the current EC2

This upgrade expects the current pilot components already installed:

- Step 2A filter
- Step 3A faster-whisper multilingual transcriber
- existing automatic filter/transcription worker (Step 3B or compatible Step 4C)
- existing FastAPI open pilot v0.2.0 or newer (v0.3.0 includes the campaign-response serialization fix)

Step 4A and Step 4B may remain installed for legacy results, but the new shared worker does not require them for new campaign matching or LLM analysis. The existing station worker and its cursor are preserved.

## Deployment order

1. From CloudShell, run `deploy/enable-radio-api-s3.sh` with the current bucket and EC2 role.
2. Upload this ZIP under the bucket's `bootstrap/` prefix.
3. Download and extract it to `/home/ec2-user/work` on EC2.
4. Run:

```bash
sudo ./deploy/install-full-backend-amazon-linux.sh
```

5. Validate the known existing mention or a live mention through the detail endpoint.
6. Run the cleanup script in dry-run mode, review it, then apply it.
7. Only after backend acceptance, update the frontend to use the new detail/highlight fields.

Exact commands are in `EC2_UPGRADE.md`.

## Cleanup

Dry-run:

```bash
sudo ./deploy/cleanup-pilot-artifacts.sh
```

Apply local cleanup only:

```bash
sudo ./deploy/cleanup-pilot-artifacts.sh --apply
```

Optionally remove old bootstrap ZIP objects older than two days:

```bash
export BUCKET_NAME='your-bucket'
export AWS_REGION='eu-north-1'
sudo -E ./deploy/cleanup-pilot-artifacts.sh --apply --purge-s3 --keep-days=2
```

The default cleanup never removes installed applications, active services, models, configuration, SQLite data, automation state, raw/filtered audio, transcripts, mentions, or intelligence results. It also limits local cleanup to radio deployment archives/directories instead of deleting unrelated ZIP files.

## Pilot limitations

- The API is still no-auth. Keep it private or restrict port 8788 to approved IPs.
- Qwen3 0.6B is intentionally small. It is useful for structured multilingual pilot analysis but is not infallible; uncertain results remain reviewable.
- “Whole transcript” means the complete contiguous retained-speech session that can be assembled from neighboring processed chunks. It does not transcribe music, does not fabricate missing audio, and uses a configurable maximum duration as a safety guard.
- Dynamic Radio Browser station onboarding and automatic capacity admission are a separate backend release. This package stabilizes conversation intelligence first.

### Optional legacy NLP disk cleanup

After v0.3 has processed real live mentions successfully, and only when the active station worker is not Step 4C, you may review:

```bash
sudo ./deploy/cleanup-pilot-artifacts.sh --remove-legacy-nlp
```

Then apply:

```bash
sudo ./deploy/cleanup-pilot-artifacts.sh --apply --remove-legacy-nlp
```

This removes the old manual Step 4A/4B application/model files. It saves disk space; it does not materially improve CPU while those tools are idle. The script refuses to run this option when an installed Step 4C worker still depends on those files.
