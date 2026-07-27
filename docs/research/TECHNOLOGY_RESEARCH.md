# Technology research (primary sources)

Research date: **2026-07-27**. Target host: AWS EC2 Graviton, **4 ARM64 vCPUs,
8 GiB RAM**, Linux `aarch64`, Python 3.11.

Every version, wheel filename, digest and quota below was read from the
project's own registry/API or from official vendor documentation on the research
date. Blog posts and third-party tutorials were not used as sources of truth.
Where a fact could not be established from a primary source it is marked
**UNVERIFIED — measure on target**, not guessed.

---

## 0. Method

| Fact class | How it was obtained |
|---|---|
| Python wheel availability, versions, licenses, dependency lists | `https://pypi.org/pypi/<name>/json` (JSON API) |
| Model file names, sizes, SHA-256, repo revision | `https://huggingface.co/api/models/<repo>?blobs=true` |
| SQS quotas and FIFO semantics | `docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/` |
| llama.cpp release tag and license | GitHub REST API `repos/ggml-org/llama.cpp` |
| Base image architectures | Docker Official Images `hub.docker.com/_/python` |
| Silero VAD capabilities | `github.com/snakers4/silero-vad` |
| YAMNet requirements | `github.com/tensorflow/models/tree/master/research/audioset/yamnet` |

---

## 1. Dependency register

### 1.1 CTranslate2 — ASR inference engine

| Field | Value |
|---|---|
| Project | OpenNMT / CTranslate2 |
| Official source | `https://opennmt.net/CTranslate2/`, PyPI `ctranslate2` |
| Version selected | **4.8.1** |
| License | MIT |
| Supported OS/arch (docs) | "OS: Linux (x86-64, **AArch64**), macOS (x86-64, ARM64), Windows (x86-64)" |
| Python | ">= 3.9" |
| **ARM64 wheel path (verified)** | `ctranslate2-4.8.1-cp311-cp311-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl` |
| cp311 aarch64 wheel present | **Yes** (also cp39/310/312/313/314) |
| Runtime memory | See §2 — **UNVERIFIED — measure on target** |
| Reason selected | Only maintained Whisper inference engine with first-party Linux/aarch64 wheels *and* CPU INT8 quantised execution. Avoids compiling on the instance. |
| Rejected alternatives | `openai-whisper` (PyTorch; torch aarch64 CPU wheels are ~90 MB+ and far slower for this workload); `whisper.cpp` (excellent, but adds a second native build+FFI surface next to llama.cpp for no measured win — reconsider if CTranslate2 benchmarks poorly); ONNX Whisper exports (no maintained multilingual pipeline with word timestamps). |

**INT8 on CPU — status.** `faster-whisper` documents `int8` and `fp32` as the CPU
compute types, and CTranslate2 exposes `compute_type="int8"` for CPU generally.
What is **not** established from primary sources is the *quality and speed* of
INT8 on aarch64 specifically: CTranslate2's aarch64 backend uses different
GEMM kernels than x86 (no AVX/VNNI path). Therefore:

> **UNVERIFIED — measure on target:** aarch64 INT8 throughput and WER versus
> `float32`. `scripts/benchmark-asr.py` must run both before
> `RADIO_ASR_COMPUTE_TYPE` is treated as a settled default. The shipped default
> is `int8` because it is the documented CPU-quantised path and has the smallest
> memory footprint, **not** because it has been benchmarked here.

### 1.2 faster-whisper — ASR pipeline

| Field | Value |
|---|---|
| Official source | `https://github.com/SYSTRAN/faster-whisper`, PyPI `faster-whisper` |
| Version selected | **1.2.1** |
| License | MIT |
| Wheel | `faster_whisper-1.2.1-py3-none-any.whl` — pure Python, architecture independent |
| Dependencies (verified from PyPI metadata) | `ctranslate2>=4.0,<5`, `huggingface-hub>=0.21`, `tokenizers>=0.13,<1`, `onnxruntime>=1.14,<2`, `av>=11`, `tqdm` |
| Python | ">= 3.9" |
| ARM64 status | Inherits from `ctranslate2`, `onnxruntime`, `av`, `tokenizers` — all four publish cp311 linux-aarch64 wheels (§1.1, §1.3, §1.4) |
| Reason selected | Word-level timestamps, language detection with probabilities, VAD integration, batched inference, and a stable Python API. Already named in the repository's `THIRD_PARTY.md`, so this is continuity, not a new vendor. |

Note: `faster-whisper` pulls `onnxruntime` unconditionally (it ships a bundled
Silero VAD). That is convenient — the pipeline image gets ONNX Runtime for free
and does not need PyTorch for VAD.

### 1.3 ONNX Runtime — VAD execution

| Field | Value |
|---|---|
| Official source | PyPI `onnxruntime` (Microsoft) |
| Version | **1.28.0** |
| License | MIT |
| `requires_python` | **>= 3.11** |
| **ARM64 wheel (verified)** | `onnxruntime-1.28.0-cp311-cp311-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl` |
| Reason selected | Runs the Silero VAD ONNX graph on CPU/aarch64 without PyTorch. |

### 1.4 PyAV — audio decode

| Field | Value |
|---|---|
| Official source | PyPI `av` |
| Version | **18.0.0** |
| License | BSD-3-Clause |
| `requires_python` | ">= 3.11" |
| **ARM64 wheel (verified)** | `av-18.0.0-cp311-abi3-manylinux_2_28_aarch64.whl` (bundles FFmpeg libraries) |
| Note | `faster-whisper` decodes audio with PyAV, so the ASR worker does **not** require an `ffmpeg` binary. The *listener* still needs the FFmpeg CLI for live-stream ingest and Opus encoding. |

### 1.5 Silero VAD — voice activity detection

| Field | Value |
|---|---|
| Official source | `https://github.com/snakers4/silero-vad`, PyPI `silero-vad` |
| Version | **6.2.1** (pure-Python wheel) |
| License | MIT |
| Runtimes | PyTorch JIT **or** ONNX Runtime |
| Sample rates | **8 000 Hz and 16 000 Hz only** |
| Model size | ~2 MB |
| Stated speed | "one audio chunk (30+ ms) in less than 1 ms on a single CPU thread" |
| Training claim | "trained on huge corpora that include over 6000 languages" |
| ARM64 status | Pure Python; the ONNX path needs `onnxruntime` (§1.3, aarch64 ✔). The PyPI package's *default* dependencies are `torch>=1.12.0` + `torchaudio>=0.12.0`; ONNX-only installs must use the `onnx-cpu` extra or load the `.onnx` file directly. |
| Reason selected | Tiny, permissive, language-agnostic, CPU-cheap; the incumbent ingestion package already uses it, so operators' expectations carry over. |

**Explicit limitation — do not paper over this.** The Silero VAD README makes
**no claim** that it separates singing from speech. It is a *voice activity*
detector: sung vocals are voice. Treating "Silero says speech" as "not a song" is
unsound and is exactly the failure mode ADR-005 exists to prevent. Silero is used
here as a *speech-presence* signal only; song rejection needs an independent
signal plus duration hysteresis.

**Dependency decision.** To avoid `torch` (~90 MB+ aarch64 wheel, large RSS) the
pipeline image installs `onnxruntime` and loads the Silero ONNX graph directly,
rather than `pip install silero-vad` with its default torch dependency.

### 1.6 YAMNet — audio event classification — **NOT DEPLOYED BY DEFAULT**

| Field | Value |
|---|---|
| Official source | `github.com/tensorflow/models/tree/master/research/audioset/yamnet` |
| What it is | MobileNet-v1 predicting **521** AudioSet event classes (527 minus 6 removed on fairness review) |
| Input | 16 kHz mono waveform; log-mel, 64 bins, 125–7500 Hz; 96×64 patches, 0.96 s hop; **≥ 975 ms** of audio needed for the first output frame |
| Quality | balanced mAP **0.306** (README) |
| Framework requirement | TensorFlow **with Keras 2** — the README states it is **incompatible with Keras 3**, which is the default from TF 2.16 onward; requires the `tf-keras` shim, plus `numpy`, `resampy`, `soundfile` |
| TF aarch64 wheel (verified) | `tensorflow-2.21.0-cp311-cp311-manylinux_2_27_aarch64.whl`, **268.8 MB** |
| `tensorflow-cpu` aarch64 | **None published** — the slim CPU-only variant has no linux-aarch64 wheel |

**Verdict: impractical for this host, and therefore not enabled.** Reasons, all
from primary sources above:

1. A **269 MB** wheel (TF full, since `tensorflow-cpu` has no aarch64 build)
   lands in a pipeline image that also carries CTranslate2 + ONNX Runtime + PyAV.
2. Resident memory. On 8 GiB the budget is already committed to
   faster-whisper (§2), llama.cpp Qwen3-0.6B-Q8 (~1.1–1.4 GiB), FastAPI, and one
   FFmpeg process per live station. A TensorFlow session is a large, poorly
   bounded additional tenant. **UNVERIFIED — measure on target** if it is ever
   revisited; it is not being guessed at here.
3. The **Keras 2 / Keras 3 conflict** requires pinning `tf-keras` against a
   TF version whose aarch64 build is the *full* package — an actively hostile
   dependency graph to keep patched.
4. `resampy` adds a `numba`/LLVM path on aarch64.

**What is implemented instead** (see `ADR-005`):

* The `AudioClassifier` interface is defined exactly as if YAMNet existed, with
  `yamnet` reserved as a named backend.
* The shipped default backend combines Silero VAD, energy/spectral statistics,
  rolling class probabilities and **hysteresis over time** — never a
  single-frame decision.
* Uncertain audio is classified `unknown` and, with
  `RADIO_TRANSCRIBE_UNCERTAIN_AUDIO=true` (default), is **transcribed**. Recall
  is protected; precision is recovered later by the transcript-level content
  classifier.
* YAMNet is **not** silently replaced with some other unreviewed audio model.
  The slot stays empty and documented until a model is researched and approved.

### 1.7 llama.cpp — local LLM runtime

| Field | Value |
|---|---|
| Official source | `https://github.com/ggml-org/llama.cpp` |
| Latest release tag on research date | **b10144** (published 2026-07-27T06:14:00Z) |
| Version selected | **b10144** (pinned) |
| Currently deployed by `deploy/install-llm-amazon-linux.sh` | `b10034` |
| License | MIT |
| Architectures | CPU builds for x86-64 and aarch64; built from source in a multi-stage Docker build, so `linux/amd64` and `linux/arm64` are both produced from one Dockerfile |
| Reason selected | Already in production here; MIT; no Python/torch runtime; native GGUF; OpenAI-compatible `llama-server`; supports `--jinja`, `--sleep-idle-seconds`, `--metrics`, and grammar/JSON-schema constrained decoding. |
| Rejected alternatives | `ollama` (extra daemon + model-management layer duplicating what we already pin); vLLM (GPU-oriented, heavy); Python-side `llama-cpp-python` (couples the LLM lifetime to a Python process — we want an isolated container). |

**Structured output.** `llama-server` supports constrained decoding via
`response_format: {"type": "json_schema", "json_schema": {...}}` and via GBNF
grammars. The current code (`app/services/llm.py`) uses only
`response_format: {"type":"json_object"}`, which constrains *syntax* but not
*shape*. The new analysis client sends a full JSON Schema and additionally
validates with Pydantic, so a server that ignores or partially honours the field
still cannot produce an unvalidated result.

> **UNVERIFIED — measure on target:** whether tag `b10144` honours
> `json_schema` for this model on aarch64 CPU end-to-end. The client is written
> to degrade safely — schema first, then bounded repair retry, then a
> deterministic fallback document — so a negative result is a performance issue,
> not a correctness one. `scripts/verify-models.py --live` covers it.

### 1.8 Qwen3-0.6B GGUF — analysis model

| Field | Value |
|---|---|
| Official source | `https://huggingface.co/Qwen/Qwen3-0.6B-GGUF` |
| License | **Apache-2.0** |
| Repo revision (pinned) | `23749fefcc72300e3a2ad315e1317431b06b590a` |
| **Only** GGUF file in the repo | `Qwen3-0.6B-Q8_0.gguf` |
| Size | **639 446 688 bytes** (~610 MiB) |
| **SHA-256 (verified via HF API)** | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031` |
| Repo last updated | 2025-05-09 |

Confirmed: the official Qwen repository publishes exactly **one** quantisation,
`Q8_0`. Any other Qwen3-0.6B GGUF quantisation comes from a third-party
re-quantiser and is out of scope.

**Non-thinking mode.** Qwen3 is a hybrid-reasoning model. The soft switch is the
`/no_think` token in the prompt (already used by `app/services/llm.py`), and the
chat template also honours `enable_thinking=false` when the server renders
templates with `--jinja`. Both are applied; the response validator additionally
strips any `<think>...</think>` block and never persists it, so reasoning cannot
leak into stored results regardless of which switch the runtime honours.

### 1.9 Whisper model weights

| Model | Repo | Revision (pinned) | File | Size | SHA-256 |
|---|---|---|---|---|---|
| Discovery (pass A) & confirmation (pass B) default | `Systran/faster-whisper-small` | `536b0662742c02347bc0e980a01041f333bce120` | `model.bin` | 483 546 902 B | `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671` |

License: **MIT**. Multilingual: the model card lists 99 languages including
`en, hi, mr, es, de, ur, ta, te, bn, gu, pa, ne`.
Non-LFS companions (`config.json`, `tokenizer.json`, `vocabulary.txt`) are pinned
by revision; only `model.bin` carries an LFS SHA-256.

`Systran/faster-whisper-medium` exists with the same language coverage and is
supported by configuration, but is **not** the default.

> **UNVERIFIED — measure on target:** whether `medium` fits alongside the LLM in
> 8 GiB and what its real-time factor is on 4 Graviton vCPUs. Do not raise the
> default without a benchmark; see `docs/QUALITY_EVALUATION.md`.

### 1.10 Supporting libraries

| Package | Version | License | cp311 linux-aarch64 wheel | Purpose |
|---|---|---|---|---|
| `pyahocorasick` | 2.3.1 | BSD-3-Clause | ✔ `...cp311-cp311-manylinux2014_aarch64...whl` | Compiled Aho–Corasick multi-pattern matcher (optional; pure-Python fallback always present) |
| `regex` | 2026.7.19 | Apache-2.0 (PSF-derived) | ✔ | Unicode script properties (`\p{Script=...}`) for script-aware boundaries |
| `boto3` | 1.43.46 (pinned, existing) | Apache-2.0 | pure Python | S3 + SQS |
| `fastapi` | 0.139.0 (pinned, existing) | MIT | pure Python | API |
| `pydantic-settings` | 2.14.2 (pinned, existing) | MIT | pure Python | config |

`pyahocorasick` and `regex` are **optional extras**. The keyword matcher detects
them at import and falls back to a tested pure-Python trie + `unicodedata`-based
boundary logic, so the core test suite never depends on a compiled extension.

### 1.11 Base images

| Image | Architectures | Use |
|---|---|---|
| `python:3.11-slim-bookworm` | Official Python images publish `amd64, arm32v5, arm32v6, arm32v7, **arm64v8**, i386, ppc64le, riscv64, s390x` | API + pipeline images |
| `debian:bookworm-slim` | same multi-arch set | llama.cpp build + runtime stage |

Bookworm (Debian 12) is chosen over Trixie because CTranslate2's aarch64 wheels
declare `manylinux_2_27`/`manylinux_2_28`; Bookworm ships glibc 2.36, which
satisfies both. Tags are explicit — never `latest` — and image digests are
recorded in `docs/DOCKER_DEPLOYMENT.md` at build time.

FFmpeg comes from the Debian `bookworm` archive (`ffmpeg` package), not a
third-party static build.

---

## 2. Memory budget on 8 GiB / 4 vCPU — the honest version

| Tenant | Estimate | Basis |
|---|---|---|
| `llm` (llama.cpp, Qwen3-0.6B-Q8, ctx 4096, q8 KV) | ~1.1–1.4 GiB | Existing unit already sets `MemoryMax=3G`; weights alone are 610 MiB |
| `transcription-worker` (faster-whisper `small`, int8, 1 worker) | ~0.9–1.4 GiB | `model.bin` is 461 MiB; int8 weights plus activations plus tokenizer |
| `listener` (Python + N × FFmpeg + N × 60 s ring buffer) | ~0.3 GiB + ~2 MiB/station ring + FFmpeg RSS/station | 60 s × 16 kHz × 2 B mono = **1.92 MB/station** ring, computed |
| `api` (FastAPI + boto3 + SQLite) | ~0.25 GiB | current production observation class |
| `planner`, `analysis-worker`, `cleanup-worker` | ~0.15 GiB each | thin Python processes |
| OS + page cache headroom | ≥ 1.0 GiB | required, not optional |

That accounts for roughly **4.5 GiB before a single station connects**. The ring
buffers are cheap; FFmpeg processes and socket buffers are not free.

**Therefore the shipped default is `RADIO_LISTENER_MAX_SESSIONS=8` and
`RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8`, not 1000.**

> This repository does not claim that one `c7g.xlarge` continuously transcribes
> 1 000 unique live stations. It claims the *control plane* holds 1 000+
> catalogue records, 1 000+ unique station subscriptions and 10 000+ keywords,
> and that stations beyond `RADIO_MAX_ACTIVE_UNIQUE_STATIONS` sit in
> `pending_capacity` until a shard with capacity exists. The synthetic load test
> (`tests/load/`) proves the *data-plane and planner* scale; it does not and must
> not be read as a live-audio capacity claim.

---

## 3. Amazon SQS — verified quotas and semantics

All values read from the SQS Developer Guide on 2026-07-27.

| Quota | Value | Source page |
|---|---|---|
| **Maximum message size** | **1 048 576 bytes (1 MiB)** | `quotas-messages` |
| Minimum message size | 1 byte | `quotas-messages` |
| Message attributes | up to 10 | `quotas-messages` |
| Batch size | up to 10 messages per request | `quotas-messages` |
| `MessageGroupId` length | 128 characters; alphanumeric + punctuation | `quotas-messages` |
| Message retention | default 4 days; min 60 s; **max 1 209 600 s (14 days)** | `quotas-messages` |
| Visibility timeout | default 30 s; min 0 s; **max 12 hours** | `quotas-messages` |
| Message timer (delay) | 0 s to 15 minutes | `quotas-messages` |
| Long polling wait | **max 20 s** | `quotas-queues` / `quotas-fifo` |
| FIFO in-flight messages | **max 120 000 per queue** | `quotas-fifo` |
| FIFO message groups | **no quota on the number of groups** | `quotas-fifo` |
| FIFO queue name | must end `.fifo`; suffix counts toward the 80-char limit | `quotas-fifo` |
| **FIFO deduplication interval** | **5 minutes** | `FIFO-queues-exactly-once-processing` |

Note the message-size quota is **1 MiB**, not the older 256 KiB figure. The
implementation still enforces a much smaller self-imposed ceiling
(`MAX_MESSAGE_BYTES = 65 536`) so that a message can never be near either limit,
and so that oversized fields fail in our validator with a clear error rather than
at the AWS API boundary.

### 3.1 The FIFO ordering constraint that shapes the design

Quoted from `FIFO-queues-understanding-logic`:

> "You may receive multiple messages from the same message group ID in one batch
> (up to 10 messages in a single call…). However, you **can't receive additional
> messages from the same message group ID in subsequent requests until**: the
> currently received messages are deleted, **or** they become visible again."

and

> "When a message is retrieved but not deleted, it remains invisible until the
> visibility timeout expires. No additional messages from the same message group
> ID are returned until the first message is deleted or becomes visible again."

**Consequence, stated bluntly:** with `MessageGroupId = station_id`, a station's
audio segments are transcribed **strictly one at a time across the entire worker
fleet**. This is a deliberate trade and it is the correct one here — segments
from a station must be appended to that station's conversation in broadcast
order, and out-of-order ASR would corrupt the assembler — but it has a hard
throughput consequence:

```
sustainable_unique_stations  ≈  worker_concurrency × (segment_seconds / per_segment_latency)
```

With 20 s segments and a hypothetical 10 s per-segment latency, one worker slot
sustains 2 stations; 4 slots sustain 8. That is the arithmetic behind the
`RADIO_LISTENER_MAX_SESSIONS=8` default. It is also why a *stuck* consumer on one
station cannot stall other stations — groups are independent — and why the
visibility-extension heartbeat matters: an expired visibility timeout on a slow
segment makes the next segment of that station visible early and risks
out-of-order delivery.

The alternative (`MessageGroupId = f"{station_id}:{sequence % K}"`) would buy
parallelism at the cost of ordering, and is documented in ADR-003 as the
explicitly rejected option with the conditions under which it could be revisited.

### 3.2 Dead-letter handling

Per the SQS model, redrive is a **queue attribute** (`RedrivePolicy` with
`maxReceiveCount`), applied by the infrastructure that creates the queues. The
application therefore **never** sends a message to a DLQ itself; it either
deletes a message (success or permanent-failure-recorded) or leaves it to
become visible again. Permanent failures are recorded in `processing_failures`
*and then deleted*, so a poison message does not consume `maxReceiveCount`
retries for a reason we already understand.

---

## 4. Temporary audio format

Decision: **Opus in Ogg**, 16 kHz mono, VBR ~24 kbps, for spooled segments.
In-process buffers stay 16-bit signed PCM at 16 kHz mono.

| Criterion | Value | Reasoning |
|---|---|---|
| Sample rate | 16 000 Hz | Whisper resamples to 16 kHz internally; Silero VAD supports only 8 k/16 k. Anything higher is discarded work. |
| Channels | 1 (mono) | Whisper is mono; radio speech content is not stereo-dependent. |
| Codec | libopus | Designed for speech at low bitrates, native 16 kHz "wideband" mode, royalty-free (BSD-licensed reference), ubiquitous in FFmpeg. |
| Bitrate | ~24 kbps VBR | ≈ 3 kB/s → a 20 s segment is ~60 kB. 8 stations × 3 kB/s ≈ 24 kB/s ≈ 2 GB/day worst case, all of it deleted within `RADIO_NO_HIT_RETENTION_MINUTES` unless it matches. |
| Timestamp precision | milliseconds, carried in DB/metadata | Never inferred from file length; the segment record owns `started_at` and `duration_ms`. |
| Rejected | WAV/PCM (~32 kB/s → 10× the disk and 10× the SQS-adjacent I/O); MP3 (worse at low bitrate, patent-history baggage); FLAC (lossless but ~8× Opus size for no ASR gain at 16 kHz). |

> **UNVERIFIED — measure on target:** WER delta between 24 kbps Opus and
> lossless PCM on the evaluation set. If measurable, raise the bitrate — the
> setting is `RADIO_SEGMENT_OPUS_BITRATE`. Lossy settings that materially damage
> ASR are not acceptable, and "materially" must be a measured number, which is
> what `docs/QUALITY_EVALUATION.md` is for.

Evidence clips (retained, user-facing) use the same encoding; they are cut from
the retained spool segments, not re-encoded from a second lossy pass.

---

## 5. Validation still required before any "production-ready" claim

| # | Item | Where it is proved |
|---|---|---|
| 1 | CTranslate2 aarch64 INT8 vs FP32: RTF and WER | `scripts/benchmark-asr.py` on the target host |
| 2 | faster-whisper `small` peak RSS with 1 and 2 workers | `docker stats` during benchmark |
| 3 | Real `RADIO_LISTENER_MAX_SESSIONS` ceiling (CPU, RSS, queue age) | staged ramp 2 → 4 → 8 → 12 |
| 4 | Per-segment end-to-end latency, and hence the FIFO group throughput ceiling | `transcription_queue_age` metric |
| 5 | llama.cpp `b10144` aarch64 build + `json_schema` honoured | `scripts/verify-models.py --live` |
| 6 | Qwen3 non-thinking mode with `--jinja` on this build | same |
| 7 | Opus 24 kbps WER delta | `docs/QUALITY_EVALUATION.md` protocol |
| 8 | Song-vs-speech confusion matrix for the default classifier | `tests/quality/` + labelled fixtures |
| 9 | SQLite WAL behaviour with 7 concurrent container writers | `tests/integration/test_sqlite_concurrency.py` + host soak |
| 10 | Spool watermark behaviour under a real disk-fill | `scripts/soak-spool.sh` |

Items 1–4 and 7–8 are the ones that could change shipped defaults. None of them
have been run here — this is a development machine, not the target host — and no
default in this repository is presented as benchmark-derived.
