# Quality evaluation

How to measure whether this system actually finds what it claims to find, and
what is **not** yet measured.

---

## 1. The honest position

**No language in this system is production-ready today.** Nothing in the
pipeline has been evaluated against labelled broadcast audio. Every threshold
in the audio classifier and every lexical cue in the content classifier is a
reasoned starting point, not a measured optimum.

That is a statement about evidence, not about the code. The design is
deliberately biased so that being wrong is survivable:

* uncertain audio is transcribed rather than discarded;
* nothing is dropped on a single frame or a single window;
* a language with no evaluated cues falls through to `unknown`, which every
  campaign policy includes;
* the LLM can never create a mention, only describe one the matcher proved.

A language becomes "supported" when it has evaluation data and a recorded
result on this page — not when someone adds a keyword in it.

---

## 2. What must be measured

| Metric | Definition | Why it matters |
|---|---|---|
| **Keyword recall** | Found mentions ÷ true mentions | A miss is invisible and permanent. The primary metric. |
| **Keyword precision** | True mentions ÷ reported mentions | False positives erode trust and cost review time. |
| **Song rejection rate** | Songs correctly excluded ÷ songs | The main precision risk (a brand in lyrics). |
| **Speech-over-music recall** | Ads over a bed found ÷ present | The main recall risk; ads are the point. |
| **Advertisement recall** | Ads found ÷ ads present | Direct product value. |
| **Announcement recall** | Announcements found ÷ present | Includes emergency alerts. |
| **ASR WER** | Word error rate on labelled audio | Upper bound on everything downstream. |
| **Processing latency** | Broadcast → mention visible | Freshness. |
| **Queue delay** | `queue_age_seconds` under load | Whether capacity is real. |

Recall is weighted above precision throughout. A false positive is visible and
cheap to dismiss; a false negative is invisible and gone forever.

---

## 3. Priority languages

Evaluated **separately**. Aggregate accuracy across languages hides exactly the
failure that matters — a system at 90% overall can be at 40% in Marathi.

| Language | Status | Cues seeded | Evaluation set | Result |
|---|---|---|---|---|
| English | Not evaluated | ✔ | — | — |
| Hindi | Not evaluated | ✔ | — | — |
| Marathi | Not evaluated | ✔ | — | — |
| Spanish | Not evaluated | ✘ | — | — |
| German | Not evaluated | ✘ | — | — |
| Mixed Hindi–English | Not evaluated | partial | — | — |

Spanish and German have **no content-classifier cues**. They will transcribe
and keyword-match (that path is language-agnostic), but content typing will
return `unknown`. That is the safe direction: `unknown` carries no policy flag
and stays included.

---

## 4. Required evaluation set

At least 20 clips per language, each labelled with: transcript, language,
content type, and every true keyword occurrence with its time span.

Mandatory categories — a set missing any of these cannot detect the
corresponding regression:

| Category | Minimum | Tests |
|---|---|---|
| Clear speech | 3 | Baseline ASR and matching |
| Advertisement (spoken) | 3 | Primary product case |
| Speech over music | 3 | The main recall risk |
| Announcement | 2 | Includes an emergency alert |
| News reading | 2 | Fast, formal speech |
| Interview | 2 | Overlapping speakers |
| Song with a brand in the lyrics | 3 | **Must produce no mention** |
| Instrumental music | 2 | Must be discarded |
| Sung advertising jingle | 2 | **Must be retained** |
| Poor-quality / low-bitrate | 2 | Realistic stream conditions |
| Fast speech | 1 | Timing accuracy |
| Proper names | 2 | The hardest ASR case for brands |
| Non-Latin script | 2 | Devanagari at minimum |

Clips must be genuinely licensed for this use. Store them **outside the
repository** and reference them by checksum.

---

## 5. Running an evaluation

The listener exposes `StationSession.ingest_pcm()` as a public seam precisely
so evaluation replays labelled audio through the *real* classification and
segmentation path, rather than a parallel implementation that could drift from
production.

```python
session = StationSession(plan, settings, classifier=build_classifier(settings), emit=collect)
for chunk in decoded_pcm_chunks(clip):
    await session.ingest_pcm(chunk)
await session.flush(reason="evaluation")
```

Then run the real matcher and content classifier over the resulting transcripts
and compare against the labels.

The synthetic audio in `tests/fixtures/audio.py` is **not** an evaluation set.
It pins decision logic — that a spoken ad over a loud bed is not classified as
`singing`, that music after speech is still discarded eventually — so a
refactor cannot silently invert a verdict. It says nothing about real accuracy.

---

## 6. Thresholds to tune, and what each trades

All in `app/services/audio_classifier.py` unless noted.

| Setting | Default | Raising it |
|---|---|---|
| `SPEECH_LOW_ENERGY_RATIO` | 0.22 | Fewer things called speech → recall down |
| `MUSIC_LOW_ENERGY_RATIO` | 0.14 | More things called music → **recall risk** |
| `SPEECH_OVER_MUSIC_FLOOR` | 0.25 | Ads over loud beds start being called `singing` → **recall risk** |
| `SPEECH_ZCR_VARIANCE` | 0.0035 | Stricter speech evidence |
| `RADIO_PURE_MUSIC_DISCARD_SECONDS` | 8 | Keeps more music → disk up, recall safer |
| `RADIO_JINGLE_MAX_SECONDS` | 30 | Retains more of each song after speech → disk up |
| `LYRIC_REPETITION_RATIO` | 0.45 | More things called lyrics → **recall risk** |

`SPEECH_OVER_MUSIC_FLOOR` deserves particular care. It exists because a music
bed physically fills the inter-word pauses the speech features measure, so
speech evidence is suppressed by the presence of music rather than by there
being less speech. Requiring the full threshold labelled spoken advertisements
over a loud bed as `singing` — a discard candidate, and precisely the mention
the product exists to catch.

The measured separation is wide (pure instrumental music scores ~0.00 on the
speech axis), so the lower bar costs little precision. Verify that against real
audio before changing it.

`RADIO_JINGLE_MAX_SECONDS` has a real, bounded cost worth stating plainly: on a
station where the DJ speaks before every track, the first 30 seconds of every
song is retained and transcribed (~90 KB of Opus). That is the price of not
silently deleting sung advertising.

---

## 7. Regression gate

Once a baseline exists, record it here and treat these as blocking:

* keyword recall must not drop by more than 2 points;
* song rejection must not drop by more than 5 points;
* speech-over-music recall must not drop at all;
* ASR WER must not rise by more than 3 points.

Any threshold change must be accompanied by the before/after table.

---

## 8. Known gaps

| Gap | Impact | Mitigation in place |
|---|---|---|
| No labelled data yet | No metric is known | Recall-first defaults throughout |
| Speech/singing separation is heuristic | Sung ads may be missed; songs may be transcribed | Jingle allowance; `unknown` is retained |
| YAMNet not deployable on this target | No audio-event model | Backend declared and refuses to start rather than silently substituting another model (ADR-005) |
| Content cues cover 3 languages | Others type as `unknown` | `unknown` carries no policy flag and stays included |
| Phonetic matching has no encoder | Level 5 unavailable | Seam exists; no unevaluated encoder shipped |
| ARM64 ASR throughput unmeasured | Capacity default is a guess | `RADIO_MAX_ACTIVE_UNIQUE_STATIONS=8`, raise empirically (OPERATIONS.md §8) |

Each gap is a decision to ship a known, bounded limitation rather than an
unmeasured claim. None of them should be closed by changing a default; they
close by producing evidence and recording it on this page.
