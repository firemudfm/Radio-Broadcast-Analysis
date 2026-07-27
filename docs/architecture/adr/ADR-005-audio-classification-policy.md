# ADR-005 — Audio classification policy (and why YAMNet is not deployed)

Status: **Accepted** · Date: 2026-07-27

## Context

The system must keep spoken advertisements, announcements, news, interviews, DJ
speech and speech over music, while discarding ordinary songs and instrumental
music before they are ever written to disk. Getting this wrong in one direction
wastes disk and CPU; getting it wrong in the other **loses a mention
permanently** — there is no second chance on a live stream.

The brief specified Silero VAD plus YAMNet, and explicitly required that YAMNet's
practicality be verified rather than assumed.

## Decision

### 1. YAMNet is not deployed. The blocker is documented, the slot is kept.

Verified findings (full detail in
[`../../research/TECHNOLOGY_RESEARCH.md §1.6`](../../research/TECHNOLOGY_RESEARCH.md)):

* YAMNet requires TensorFlow **with Keras 2**; its README states it is
  **incompatible with Keras 3**, the default since TF 2.16.
* `tensorflow-cpu` publishes **no** linux-aarch64 wheel. The only aarch64 option
  is the full `tensorflow` wheel: **268.8 MB** (`tensorflow-2.21.0-cp311-cp311-manylinux_2_27_aarch64.whl`).
* That lands in a pipeline image already carrying CTranslate2, ONNX Runtime and
  PyAV, on a host with 8 GiB shared with llama.cpp and faster-whisper
  (§2 memory budget).
* `resampy` pulls a `numba`/LLVM toolchain on aarch64.

The `AudioClassifier` interface is defined exactly as if YAMNet existed and
reserves `yamnet` as a named backend that raises a clear
`ClassifierUnavailable` describing the blocker. **YAMNet is not silently
replaced with some other unreviewed audio model** — the slot stays empty until a
replacement is researched and approved.

### 2. Default backend: `vad_energy` — multi-signal, with hysteresis

Never a single-frame decision. Per 32 ms frame the classifier computes:

| Signal | Purpose |
|---|---|
| Silero VAD probability (ONNX, 16 kHz) | speech presence |
| Short-term RMS energy + a rolling noise floor | silence detection |
| Spectral flatness | tonal/musical vs broadcast-speech texture |
| Zero-crossing rate | fricative/voicing balance |
| Low/high band energy ratio | music's stronger sustained low-band content |
| VAD-probability **variance** over a rolling window | speech is bursty; sustained singing is not |

Frames aggregate into a rolling window (`RADIO_CLASSIFIER_WINDOW_SECONDS`,
default 3.0) and a state machine with **hysteresis**: entering a discard state
requires sustained confidence (`RADIO_PURE_MUSIC_DISCARD_SECONDS`, default 8;
`RADIO_SILENCE_END_SECONDS`, default 12); leaving it requires only a short
speech burst. Asymmetry is deliberate — cheap to re-enter recording, expensive to
have missed a mention.

Output classes: `silence`, `music`, `singing`, `speech`, `speech_over_music`,
`jingle`, `unknown`.

**Silero alone does not distinguish speech from singing.** Its README makes no
such claim; sung vocals are voice. Singing is inferred from *sustained* VAD
positivity combined with high tonality and low VAD variance over ≥
`RADIO_PURE_MUSIC_DISCARD_SECONDS`, never from VAD alone. This is a heuristic,
its accuracy is a measured number in `docs/QUALITY_EVALUATION.md`, and it is
biased toward retention.

### 3. Quality-first policy

| Audio | Action |
|---|---|
| Clear speech | transcribe |
| Speech over music | transcribe |
| Spoken advertisement with music bed | transcribe |
| Announcement / news / interview / DJ speech | transcribe |
| **Unknown or uncertain** | **transcribe** (`RADIO_TRANSCRIBE_UNCERTAIN_AUDIO=true`) |
| Short jingle adjacent to spoken advertising | retain as advertisement context |
| Pure instrumental music, sustained confidence ≥ 8 s | discard from RAM |
| Long-form singing, sustained confidence, no spoken context | discard from RAM |
| Silence ≥ 12 s | close conversation; discard |

Discard means **discarded from the ring buffer** — never written to disk, never
uploaded. Ordinary music does not touch the spool merely because it passed
through RAM.

### 4. Two-stage precision

Audio classification is tuned for **recall**; precision is recovered *after*
transcription by the transcript-level content classifier (ADR-010). A song that
survives audio classification is caught as `song_lyrics` from its transcript and
excluded by campaign policy. The cost of a false-retain is one cheap ASR pass;
the cost of a false-discard is a permanently lost mention.

### 5. Confidence is a first-class field

Every segment records `content_class`, `content_class_confidence` and the
per-signal votes. Downstream consumers can filter on confidence, and evaluation
can measure the classifier without re-running audio.

## Alternatives considered

1. **YAMNet as specified.** Rejected on the verified grounds above. Reversible:
   implement the `yamnet` backend if a Keras-2-compatible aarch64 path with a
   bounded memory profile appears, or if the host gains RAM.
2. **Silero VAD alone.** Rejected: it is a voice-activity detector, not a
   music/speech discriminator. Using it alone would discard every song with
   vocals *and* every sung advertisement, or nothing at all.
3. **A different pretrained audio-event model (PANNs, AST, BEATs).** Rejected
   for now: substituting an unreviewed model for YAMNet is exactly what the
   brief forbids. Each would need its own research pass, licence review and
   aarch64 verification.
4. **Transcribe everything, classify only from text.** Rejected: 24 h/day of
   music through faster-whisper on 4 vCPUs is not affordable, and it would fill
   the spool with song audio.
5. **Fixed thresholds without hysteresis.** Rejected: flapping at boundaries
   fragments conversations and produces one-frame discards mid-sentence.

## Consequences

* Some music reaches ASR. That is the intended trade and it is bounded by the
  8 s/12 s hysteresis.
* Classifier accuracy is a *measured* number, not a claim. Until
  `docs/QUALITY_EVALUATION.md` is populated from labelled fixtures, no accuracy
  figure is asserted anywhere in this repository.
* No perfect song/advert separation is claimed. Sung advertising jingles are
  handled by *policy* (short + adjacent spoken advertising + campaign opt-in),
  not by a classifier that can reliably tell a jingle from a chorus.
* The pipeline image stays small: ONNX Runtime + a 2 MB model, no TensorFlow.

## Operational risks

| Risk | Mitigation |
|---|---|
| Music-heavy station floods the spool | Watermarks pause admission; per-station segment-rate metric; `content_class` distribution is logged |
| Talk station misclassified as music | Hysteresis + `unknown → transcribe`; per-station override `RADIO_STATION_CLASSIFIER_PROFILE` |
| Silero ONNX model missing | `/readyz` fails closed at startup; the classifier never silently degrades to "everything is speech" without saying so in health |
| Thresholds tuned for one market | All thresholds are settings; the evaluation harness re-tunes per market |

## Security impact

Minimal. The Silero ONNX graph is a pinned local file with a recorded digest,
loaded from a read-only mount; no network access at classification time. Audio
never leaves the host during classification. Refusing TensorFlow also removes a
very large third-party dependency tree from the attack surface.

## Cost impact

Strongly positive. Discarding music before the spool avoids disk, avoids ASR CPU
and avoids S3. Silero costs <1 ms per 32 ms frame on one CPU thread (project
claim), i.e. well under 5 % of one core per station.

## Test requirements

* Pure music (sustained) → discarded; short music burst inside speech → retained.
* Long song → discarded; song ending and speech starting → retained from the
  first speech frame, with pre-roll.
* Spoken advertisement over a music bed → retained.
* Announcement → retained.
* Speech over a song intro → retained.
* Uncertain/low-confidence audio → retained (policy assertion).
* Hysteresis: a single anomalous frame never flips state.
* `yamnet` backend raises `ClassifierUnavailable` with a message naming the
  blocker (regression test against silently substituting a model).
* Ring buffer never grows beyond its bound during a long music passage.
* Confidence is populated on every classified segment.

## Reversal strategy

`RADIO_AUDIO_CLASSIFIER=passthrough` classifies everything as `speech` and
transcribes it all — a diagnostic escape hatch when classification is suspected
of losing content. Costly, deliberately so, and logged as a WARNING every cycle.
Threshold changes are configuration-only. Implementing `yamnet` later requires no
interface change.
