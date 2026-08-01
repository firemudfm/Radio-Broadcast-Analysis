# Model management

Which model files the system uses, where they come from, and why none of them
are in a Docker image.

---

## 1. Why models are not baked into images

Three reasons, in order of importance:

1. **Licensing.** The weights carry their own licences (MIT for the Whisper
   conversion, Apache-2.0 for Qwen3). Baking them into an image puts a
   separately-licensed binary into every registry layer and every pull.
2. **Reproducibility.** An image that downloads a model at start-up makes every
   restart depend on a third party being up, and silently changes what
   "restart" means if the upstream file changes.
3. **Size.** ~1.1 GB across three files. Every image push, pull and layer cache
   would carry it.

So: images contain code, `/models` is a read-only bind mount, and fetching is
an explicit, verified step.

## 1a. There is no automatic download. Anywhere.

This is a hard policy, not a default that can be flipped:

* **No model is downloaded during Docker build.**
* **No model is downloaded during container startup.**
* **No model is downloaded during API startup.**
* **No model is downloaded during worker startup.**

`FasterWhisperEngine` loads only from a local directory. It holds no HTTP
client and never passes a repository id to `WhisperModel`, so there is no code
path that can reach the Hub. A missing model raises a named permanent
`ModelVerificationError` (`model_verification_failed`) quoting the exact
commands below.

The LLM container fails fast the same way, exiting `78` with host-side
instructions; it has no Python interpreter and only a read-only `/models`
mount.

> An earlier revision of this document described an `ALLOW_MODEL_DOWNLOAD=1`
> escape hatch. **It never existed** — no such setting was ever read, and the
> code branch behind it was unreachable. Both the claim and the dead branch
> have been removed rather than implemented: runtime downloading bypasses
> `models.lock.json`, makes a restart depend on a third party being reachable,
> makes start-up unbounded, and can place an unverified model on the runtime
> path.

Acquisition is always two explicit operator steps: **download**, then
**verify**. `models.lock.json` remains the single source of provenance and
expected files.

---

## 2. The lock file

`models.lock.json` pins every artefact: repository, revision, filename, size
and SHA-256 where the provider publishes one.

Changing a revision here changes transcription or analysis output. That makes
it a reviewable change, not a runtime detail — which is the point of having a
lock file at all.

| Model | Purpose | Size | Licence |
|---|---|---|---|
| `Systran/faster-whisper-small` | ASR, both passes | 483.5 MB | MIT |
| `Qwen/Qwen3-0.6B-GGUF` (`Q8_0`) | Conversation analysis | 639.4 MB | Apache-2.0 |
| `snakers4/silero-vad` (`silero_vad.onnx`) | Voice-activity signal | 2.3 MB | MIT |

Two honest notes about pinning:

* **Silero has no published digest.** The lock file pins its size (confirmed
  from the tag's `Content-Length`) and leaves `sha256` null. Deriving a digest
  from one local download would assert a verification that never happened.
  Size alone still catches truncation.
* **Whisper's non-LFS companions** (`config.json`, `tokenizer.json`,
  `vocabulary.txt`) are pinned by repository revision rather than digest,
  because the Hub only publishes LFS digests.

---

## 3. Downloading

```bash
make models-plan                     # show what would be fetched, fetch nothing
make models                          # everything, ~1.1 GB
make models-asr                      # ASR + VAD only (skip the 610 MB LLM)
```

On the deployment host, as the container user. The role selector is
`--role`, repeatable — there is no `--asr`, `--llm` or `--vad` flag:

```bash
# ASR only
python3 scripts/download-models.py \
  --root /var/lib/radio/models \
  --role asr

# LLM only
python3 scripts/download-models.py \
  --root /var/lib/radio/models \
  --role llm

# VAD only (optional; the classifier degrades to energy signals without it)
python3 scripts/download-models.py \
  --root /var/lib/radio/models \
  --role vad

# Everything (~1.1 GB)
python3 scripts/download-models.py \
  --root /var/lib/radio/models
```

Run as the container user so the files land with the uid the containers use:

```bash
sudo -u '#10001' python3 scripts/download-models.py \
  --root /var/lib/radio/models --role asr
```

The downloader verifies each file's digest **before** moving it into place, and
writes to a temporary name then renames, so an interrupted run can never leave
a half-written file that looks complete.

Both scripts are stdlib-only, so they run on a bare host before any dependency
is installed.

---

## 4. Verifying

Verification uses the same `--role` selector, so it can be scoped to whatever
was just downloaded:

```bash
python3 scripts/verify-models.py --root /var/lib/radio/models --role asr
python3 scripts/verify-models.py --root /var/lib/radio/models --role llm
python3 scripts/verify-models.py --root /var/lib/radio/models --role vad

# Everything
make models-verify
sudo -u '#10001' python3 scripts/verify-models.py --root /var/lib/radio/models
```

Run this after every download and **before starting workers**. A worker that
starts with a truncated or substituted model produces plausible-looking wrong
output rather than an error — which is exactly the failure this exists to
catch, and why there is no automatic-download path that could skip it.

```bash
scripts/verify-models.py --root /var/lib/radio/models --quick   # size only, fast
scripts/verify-models.py --root /var/lib/radio/models --role asr
```

An absent VAD model is reported and tolerated: the classifier degrades to
energy-only signals, which is a documented quality trade-off (ADR-005), not a
correctness failure. An absent ASR or LLM model is a hard failure.

---

## 5. On-disk layout

```
/var/lib/radio/models/
├── asr/
│   └── Systran__faster-whisper-small/
│       ├── model.bin            483 546 902 B
│       ├── config.json
│       ├── tokenizer.json
│       └── vocabulary.txt
├── qwen/
│   └── Qwen3-0.6B-Q8_0.gguf     639 446 688 B
└── vad/
    └── silero_vad.onnx            2 327 524 B
```

The `/` in a Hugging Face repository id becomes `__` in the directory name, so
no repository id can ever traverse out of the model root.

Mounted read-only into every worker (`/models:ro`). A compromised worker cannot
alter the weights it is running.

---

## 6. Changing a model

The ASR model is configurable, and `Systran/faster-whisper-medium` is supported
by configuration. It is **not** the default, and switching is not a
one-line change:

1. Add the new entry to `models.lock.json` with its verified revision and
   digest.
2. Download and verify it.
3. Benchmark on the **target hardware** — real-time factor on 4 Graviton
   vCPUs, and peak RSS alongside the LLM in 8 GiB.
4. Run the quality evaluation in [QUALITY_EVALUATION.md](QUALITY_EVALUATION.md)
   and compare against the current baseline.
5. Only then change `RADIO_ASR_MODEL`.

Whether `medium` fits alongside the LLM in 8 GiB is **unmeasured**. Do not
raise the default on the assumption that a bigger model is better: an ASR
worker that pushes the host into swap makes every station worse, and dropped
live audio is unrecoverable in a way a slower queue is not.

---

## 7. Provenance

All three artefacts were resolved from their official sources on the research
date, and the URLs the downloader constructs were confirmed to return HTTP 200
with `Content-Length` matching the lock file exactly:

| File | Expected | Confirmed |
|---|---|---|
| `Qwen3-0.6B-Q8_0.gguf` | 639 446 688 | ✔ |
| `model.bin` | 483 546 902 | ✔ |
| `silero_vad.onnx` | 2 327 524 | ✔ |

Full provenance, rejected alternatives and the ARM64 wheel verification are in
[research/TECHNOLOGY_RESEARCH.md](research/TECHNOLOGY_RESEARCH.md).

What remains **unverified and must be measured on the target host**:

* end-to-end ASR accuracy and real-time factor on Graviton;
* whether llama.cpp `b10144` honours `json_schema` constrained decoding for
  this model on aarch64 (the analysis client degrades safely if not: schema,
  then one bounded repair retry, then a deterministic fallback);
* peak resident memory of both models running together.
