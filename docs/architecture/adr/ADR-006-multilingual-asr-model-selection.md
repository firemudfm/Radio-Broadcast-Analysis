# ADR-006 — Multilingual ASR model selection and two-pass strategy

Status: **Accepted** · Date: 2026-07-27

## Context

Broad multilingual transcription is required (English, Hindi, Marathi, Spanish,
German, mixed Hindi-English, non-Latin scripts), on 4 ARM64 vCPUs shared with a
local LLM, with exact evidence text and timestamps preserved.

## Decision

### 1. Engine

`faster-whisper` **1.2.1** on `ctranslate2` **4.8.1**. Both MIT. CTranslate2
publishes a verified cp311 linux-aarch64 wheel
(`manylinux_2_27_aarch64.manylinux_2_28_aarch64`); `faster-whisper` is pure
Python. `faster-whisper` decodes audio with PyAV, so the ASR worker needs no
`ffmpeg` binary.

### 2. Default profile — and what "default" means here

```
RADIO_ASR_MODEL=Systran/faster-whisper-small
RADIO_ASR_CONFIRMATION_MODEL=Systran/faster-whisper-small
RADIO_ASR_DEVICE=cpu
RADIO_ASR_COMPUTE_TYPE=int8
RADIO_ASR_CPU_THREADS=2
RADIO_ASR_BEAM_SIZE=1
RADIO_ASR_CONFIRMATION_BEAM_SIZE=5
```

Model pinned at revision `536b0662742c02347bc0e980a01041f333bce120`,
`model.bin` SHA-256
`3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671`.

These are **starting points chosen to be safe on 8 GiB, not benchmark-derived
optima.** No claim is made that `small` is accurate enough for any given
language, or that `int8` on aarch64 matches `int8` on x86 — CTranslate2's aarch64
backend uses different GEMM kernels and has no AVX/VNNI path. `medium` is
supported by configuration and is **not** the default because it has not been
shown to fit alongside the LLM here. Raising it requires a benchmark
(`scripts/benchmark-asr.py`), not an opinion.

### 3. Two-pass strategy

**Pass A — discovery** (every speech segment): `beam_size=1`, VAD filter on,
`word_timestamps=False`, language auto-detected with probability recorded, no
initial prompt. Cheap and high-recall; its job is to find candidates.

**Pass B — confirmation** (only conversations with a fuzzy or phonetic
candidate): `beam_size=5`, `word_timestamps=True`, station language hint,
`condition_on_previous_text=False`, and an `initial_prompt` built from the
station's approved canonical keyword surface forms.

Pass B runs on the *conversation*, not per segment, so its cost scales with
matches rather than with airtime.

**The prompt is a controlled surface, not free text.** Only canonical values and
approved aliases from the station's keyword index are included; entries are
length-capped, de-duplicated, joined with a fixed separator, and the total is
capped at `RADIO_ASR_PROMPT_MAX_CHARACTERS` (default 400). A keyword cannot
inject arbitrary text into the decoder.

Exact-match candidates skip pass B — they already have verbatim evidence.

### 4. Preserved outputs

Original-language transcript (never translated by default), detected language,
language probability, segment and word timestamps where produced, model name,
model revision, compute type, beam size, and `asr_pass` (`a` or `b`). Stored on
`transcripts`, so a model change is auditable per row and re-analysis can be
scoped to the affected version.

Code-switching is handled by *not* forcing a language in pass A and by matching
against the combined multilingual index rather than a single-language one.

### 5. Model management

Models are **not** baked into images. `/var/lib/radio/models` is mounted
read-only into the pipeline container. `models.lock.json` pins repo, revision,
filenames, sizes, SHA-256 and licence. `scripts/download-models.py` fetches and
verifies; `scripts/verify-models.py` verifies without downloading and is what
`/readyz` and the container healthcheck rely on. Automatic download during
container startup is refused unless `ALLOW_MODEL_DOWNLOAD=1` is explicitly set.

## Alternatives considered

1. **`openai-whisper` (PyTorch).** Rejected: torch aarch64 CPU is large and
   slower for this workload; no word-timestamp/VAD pipeline of comparable
   maturity.
2. **`whisper.cpp`.** Genuinely competitive on ARM and worth revisiting if
   CTranslate2 benchmarks poorly. Rejected for now: a second native build + FFI
   surface next to llama.cpp, for an unmeasured win.
3. **`distil-large-v3`.** Rejected: **English-only**, disqualifying for a
   multilingual product.
4. **Cloud ASR (Transcribe/Whisper API).** Rejected: the product constraint is
   local processing; also per-minute cost across continuous streams.
5. **Single-pass with `beam_size=5` everywhere.** Rejected: several times the CPU
   for every segment, when >95 % of segments contain no candidate.
6. **`medium` as default.** Rejected until benchmarked — see above.

## Consequences

* Two model handles may be loaded if the confirmation model differs; defaulting
  them to the same model means one resident model.
* Pass B cost tracks match volume, which is the desirable scaling property.
* Word timestamps exist only where pass B ran; exact matches carry segment-level
  timestamps. The API distinguishes these rather than pretending to word
  precision it does not have.
* Model revision is recorded per transcript, so a future re-run can target only
  rows produced by an older model.

## Operational risks

| Risk | Mitigation |
|---|---|
| Model files missing at startup | `/readyz` fails; the worker refuses to consume; explicit, not silent |
| Model download during startup saturating the host | Refused without `ALLOW_MODEL_DOWNLOAD=1`; the documented path is the offline script |
| ASR slower than real time → queue growth | `transcription_queue_age_seconds` is the primary alert; the planner reduces admission |
| INT8 on aarch64 degrades quality | `RADIO_ASR_COMPUTE_TYPE` is a setting; the benchmark script compares int8/float32 side by side |
| Thread oversubscription with the LLM | `RADIO_ASR_CPU_THREADS=2` plus Compose CPU limits on both services |

## Security impact

* Model files are read-only mounts with verified digests — a tampered model
  fails `verify-models.py` and the worker refuses to start.
* No network access is required at inference time; the pipeline container does
  not need egress to Hugging Face in normal operation.
* `initial_prompt` is a controlled allowlist (see above), so keyword content
  cannot steer the decoder arbitrarily.
* Transcript bodies are never logged at INFO.

## Cost impact

CPU is the scarce resource. Pass A ≈ 1 decode per 20 s of retained speech per
station. Pass B ≈ 1 decode per candidate conversation. No per-request cost — the
model is local.

## Test requirements

* A `FakeTranscriber` implements the same protocol; the unit suite never loads a
  real model.
* Language hints are passed through to the engine; `None` in pass A.
* Code-switched fixture yields segments retaining both scripts.
* Confirmation escalation happens only for fuzzy/phonetic candidates, never for
  exact matches.
* Word timestamps are monotonic and lie inside the segment bounds; violations
  are rejected rather than stored.
* Model error → `TranscriptionFailed(retryable=True)`; corrupt audio →
  permanent.
* `models.lock.json` matches the digests recorded in `TECHNOLOGY_RESEARCH.md`
  (a test, so the two documents cannot drift).
* `verify-models.py` detects a truncated or altered file.
* Prompt construction is capped and de-duplicated (injection-resistance test).

## Reversal strategy

Every knob is configuration. Reverting to the legacy externally-managed ASR is
`RADIO_PIPELINE_MODE=legacy`. Changing model is: update `models.lock.json`, run
`download-models.py`, restart the worker. Because `transcripts` records the model
revision per row, a bad model change is identifiable and re-runnable rather than
silently mixed into history.
