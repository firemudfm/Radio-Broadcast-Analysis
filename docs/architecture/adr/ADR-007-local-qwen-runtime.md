# ADR-007 — Local Qwen runtime and structured analysis

Status: **Accepted** · Date: 2026-07-27

## Context

Qwen3 0.6B via llama.cpp is already in production (`radio-llm.service`, tag
`b10034`, `Qwen3-0.6B-Q8_0.gguf`, bound to `127.0.0.1:8790`). The current client
(`app/services/llm.py`) asks for `response_format: {"type":"json_object"}`,
parses leniently (regex-extract the first `{...}` on failure), clamps enums, and
has one attempt with no circuit breaker and no output-schema validation.

## Decision

### 1. Runtime

llama.cpp pinned at tag **`b10144`** (latest release on 2026-07-27; currently
deployed: `b10034`), MIT, built from source in a multi-stage Docker build
producing `linux/amd64` and `linux/arm64` from one Dockerfile. Model
`Qwen3-0.6B-Q8_0.gguf` (Apache-2.0), repo revision
`23749fefcc72300e3a2ad315e1317431b06b590a`, **639 446 688 bytes**, SHA-256
`9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031`. Confirmed:
that is the **only** GGUF quantisation the official Qwen repository publishes.

Container rules: model mounted read-only at
`/models/qwen/Qwen3-0.6B-Q8_0.gguf` (host `/var/lib/radio/models:/models:ro`),
never baked into the image; port 8790 exposed on the Compose network only and
**never** published to the host; non-root runtime; `no-new-privileges`; bounded
CPU and memory; `--sleep-idle-seconds` to release memory when idle.

### 2. When Qwen is called — exactly once per confirmed conversation

Not per segment, not per keyword, not per campaign. One physical conversation
produces one analysis, mapped to every matching campaign and keyword. This is the
single largest CPU saving in the design, and it is enforced by
`UNIQUE(analysis_job_id)` plus `UNIQUE(conversation_id)` rather than by
convention.

### 3. Structured output — defence in depth

Three independent layers, because any one of them can fail:

1. **Constrained decoding.** The request sends
   `response_format: {"type": "json_schema", "json_schema": {...}}` with the full
   schema. Whether tag `b10144` honours this for this model on aarch64 is
   **UNVERIFIED — measure on target** (`scripts/verify-models.py --live`).
2. **Pydantic validation.** The response is validated against
   `LlmAnalysisResultV1` regardless of what the server did. Unknown fields are
   rejected (`extra="forbid"`).
3. **Semantic validation.** Beyond types:
   * every `evidence[].text` must appear **verbatim** in the transcript
     (normalised comparison, then a verbatim span lookup);
   * `evidence[].start_ms`/`end_ms` must fall inside the conversation bounds;
   * `confidence` ∈ [0, 1];
   * enums must be members of their allowlist;
   * `summary` and `key_points` are length-capped.

   Evidence failing verbatim or bounds checks is **dropped**, not repaired. An
   analysis with zero surviving evidence is marked `needs_review`.

On invalid JSON: **one** bounded repair retry with a shortened prompt and the
parser error appended. On repeated failure: a deterministic fallback document
(`status="fallback"`, empty analysis fields, `needs_review=true`,
`confidence=0.0`) — never a partially-parsed result presented as complete.

### 4. Reasoning is never stored

`/no_think` is sent (the Qwen3 soft switch, already used today) and the server
runs with `--jinja` so `enable_thinking=false` in the chat template is honoured.
Independently of both, the response validator strips any `<think>…</think>`
block before parsing and the stripped content is never persisted or logged.
Three mechanisms, because each depends on runtime behaviour we have not verified
on this build.

### 5. Resilience

| Control | Setting | Default |
|---|---|---|
| Request timeout | `RADIO_LLM_TIMEOUT_SECONDS` | 90 (5..300) |
| Max output tokens | `RADIO_LLM_MAX_OUTPUT_TOKENS` | 480 (64..2048) |
| Max input characters | `RADIO_LLM_MAX_INPUT_CHARACTERS` | 40 000 |
| Circuit breaker threshold | `RADIO_LLM_CIRCUIT_FAILURE_THRESHOLD` | 5 |
| Circuit breaker cooldown | `RADIO_LLM_CIRCUIT_RESET_SECONDS` | 60 |
| Repair retries | `RADIO_LLM_REPAIR_RETRIES` | 1 (0..2) |

The circuit breaker is closed → open after N consecutive failures → half-open
after the cooldown → closed on one success. While open, analysis jobs return the
deterministic fallback and remain **retryable**, so a temporarily dead LLM
degrades the product instead of poisoning the queue.

### 6. Test posture

The unit suite uses a deterministic `FakeLlmClient`. **No test downloads 610 MB.**
A separate opt-in `tests/integration/test_llm_live.py` runs only with
`RADIO_LLM_LIVE_TEST=1` and a reachable server, and is skipped by default in CI.

## Alternatives considered

1. **Keep `json_object` + lenient regex parsing.** Rejected: it is the current
   behaviour and it cannot guarantee shape. A regex-extracted `{...}` from a
   truncated response is a plausible-looking wrong answer.
2. **GBNF grammar instead of JSON Schema.** Kept as a fallback if `json_schema`
   proves unreliable on `b10144`; the client can emit either. Not the default
   because JSON Schema is the portable form.
3. **A larger model (Qwen3-1.7B/4B).** Rejected: memory budget (§2 of the
   research doc). Revisit if the host grows.
4. **`llama-cpp-python` in-process.** Rejected: couples model lifetime to a
   Python worker and prevents independent CPU/memory limits and restarts.
5. **`ollama`.** Rejected: another daemon and model-management layer duplicating
   the pinning we already do.
6. **LLM-based keyword matching as the primary matcher.** Rejected emphatically —
   see ADR-010. The LLM never creates a mention.

## Consequences

* One LLM call per conversation caps LLM CPU by *match volume*, not airtime.
* Schema validation may reject outputs a lenient parser would have accepted.
  That is the point; `needs_review` and the fallback document make rejection
  visible rather than silently degrading data.
* llama.cpp is pinned by tag and built from source, so the build is reproducible
  but not instant (~4–8 min on 4 vCPUs); the LLM image is built rarely.
* The LLM is unreachable from outside the Compose network by construction.

## Operational risks

| Risk | Mitigation |
|---|---|
| LLM cold start after idle sleep | Timeout tuned above cold-start; first request may be slow but is retryable |
| Model file missing/corrupt | `verify-models.py` at startup; `/readyz` reports `llm: unavailable` |
| Repeated schema failures | Circuit breaker + fallback + `processing_failures` rows with the parser error |
| Memory pressure from LLM + ASR together | Compose memory limits on both; `--sleep-idle-seconds`; `MemoryMax` equivalent per service |
| Port 8790 accidentally published | `compose.prod.yaml` has no `ports:` for `llm`; a test asserts this |

## Security impact

* No model weights, credentials or `.env` files in the image.
* Read-only model mount; non-root runtime; `no-new-privileges`; no Docker socket;
  no host network.
* The LLM has **no** egress requirement.
* Transcript content is sent only to localhost/Compose-network.
* Chain-of-thought is stripped and never persisted — three independent mechanisms.
* Prompt-injection posture: a broadcast could contain text instructing the model.
  Because the *only* thing that creates a mention is the deterministic matcher
  (ADR-010), and because evidence must be verbatim from the transcript, a
  successful injection can at worst corrupt a summary field — it cannot fabricate
  a mention, a campaign mapping, or evidence.

## Cost impact

Zero marginal cost — local inference. CPU is bounded by `CPUQuota`/`cpus`. Idle
sleep releases memory between conversations.

## Test requirements

* Valid structured result → parsed, validated, stored.
* Invalid JSON → one repair retry → deterministic fallback on second failure.
* Evidence not present in the transcript → dropped; result flagged
  `needs_review`.
* Evidence timestamps outside conversation bounds → dropped.
* `confidence` out of range → rejected.
* Unknown enum → rejected.
* Extra field → rejected (`extra="forbid"`).
* `<think>` block present → stripped, never stored, never logged.
* Timeout → `retryable` error, message stays visible.
* Circuit breaker opens after N failures and half-opens after the cooldown.
* Exactly **one** LLM call per conversation, asserted by call count.
* `models.lock.json` digest matches the value in `TECHNOLOGY_RESEARCH.md`.

## Reversal strategy

`RADIO_LLM_ENABLED=false` disables analysis entirely; mentions are still created,
stored and served with `analysis.status="disabled"` — the keyword product keeps
working without the LLM. Reverting the client to lenient parsing is a
configuration flag (`RADIO_LLM_STRICT_SCHEMA=false`) retained for one release as
an emergency escape hatch, logged as a WARNING on every use.
