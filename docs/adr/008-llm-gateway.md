# ADR-008: The LLM gateway: append-only requests, a budget that cannot be exceeded, replayable turns

- **Status:** Accepted (2026-11-06)
- **Amends:** PLAN §3.1 and §10 (where the gateway lives and how it retries)

## Context

Every agent role reaches the model through one loop (ADR-002). That loop has to satisfy
four masters at once:

- **The API's rules for this model family.** Thinking is adaptive and cannot be disabled.
  Sampling parameters, forced `tool_choice` and assistant prefill are rejected. An edited
  history invalidates the thinking blocks after the edit (preserved thinking). A refusal
  can be re-run server-side on another model, with its own echo rules.
- **The budget invariant.** A run's spend never exceeds its reservation, so the ledger has
  to know what each turn could cost before sending it, and what it did cost after.
- **At-least-once effects (ADR-007).** An invocation in flight at a crash runs again, and
  must not pay again for turns that already came back.
- **Offline tests.** CI has no API key, and must still prove what the gateway sends.

## Decision

1. **A layer of its own.** `msa.llm` sits below `msa.agents` and above `msa.config`
   and `msa.core`. It is the only package that imports `anthropic` or `httpx2`, which a
   contract test checks (`tests/contract/test_llm_requests.py`). This replaces PLAN §10's
   `runtime/gateway.py`: agents call the gateway, so it cannot live above them.
2. **Dicts at the port.** `MessagesClient` takes and returns JSON-shaped dicts. The
   gateway validates every response into the SDK's `BetaMessage` before reading it. The
   cassette, the response store and the fake client therefore persist and replay exactly
   what crossed the wire, and a request's digest is stable.
3. **One request shape, checked by property tests.** Requests carry adaptive thinking;
   explicit per-role effort; a strict JSON-schema answer format; strict tools sorted by
   name; a cached, byte-stable system prompt; `fallbacks: "default"`; and the betas from
   `msa.toml`. They never carry sampling parameters, `tool_choice` or prefill. Generated
   multi-turn scripts check that every request has this shape, that system, tools and
   configuration never change within an invocation, and that `messages` only grows.
4. **Edits fail loudly.** The transcript is append-only by type, and every request sends
   `thinking.block_binding.prefix_mismatch_behavior: "error"` (beta
   `thinking-binding-controls-2026-08-01`). An accidental edit is a 400 on the spot
   instead of reasoning silently dropped. A model-binding mismatch after a downgrade
   fallback is not an error in this mode; it is counted per turn as `thinking_dropped`.
5. **Fallbacks are billed per attempt.** Spend is summed over `usage.iterations`, each
   attempt at its own model's price. Top-level usage covers only the serving attempt. A
   served model missing from the price table is charged the highest configured rate of
   each component. Echo rules: before the last `fallback` block only text is kept, and
   no `tool_use` from a declined attempt is ever executed.
6. **A turn is planned so its worst case fits.**
   - `max_tokens` is the largest output the remaining reservation can buy, after pricing
     every input token at its worst rate (uncached, or written to either cache).
   - The input count starts as a deliberately high estimate. It is counted exactly
     (`count_tokens` is free) only when the estimate makes the budget bind.
   - A turn that cannot afford `min_output_tokens` is not sent.
   - `task_budget` is sent only when the reservation is at least 20,000 tokens, which is
     the API's minimum.
7. **Retries are split by who can do them.**
   - Before a response starts, the SDK retries 408/409/429/5xx and connection errors,
     honouring `retry-after`.
   - After the stream starts it cannot retry. That covers `error` events (a bare
     `APIStatusError` with status 200), transport failures while reading the body (raw
     `httpx2` exceptions), and streams cut off before `message_stop`. The adapter maps
     these to `LlmError(mid_stream=True)`, and the gateway retries them with doubling
     backoff, up to `api.max_retries` times.
   - Everything else ends the invocation as `InvocationFailed` with its spend so far.
8. **Paid turns are replayed, not re-bought.** Each response is stored under
   `(effect id, turn)` with its request digest, durably, before the gateway acts on it.
   A re-executed invocation replays every turn whose request is byte-identical, and asks
   again from the first turn that differs.
9. **Always streamed, read whole.** `messages.stream()` plus `get_final_message()` keeps
   64k-token turns clear of HTTP timeouts. The gateway never acts on partial output, so
   `eager_input_streaming` would buy nothing, and it would give up the server-side
   strict-schema validation that tools get.
10. **The base URL is explicit.** It comes from `msa.toml`, so an `ANTHROPIC_BASE_URL`
    set by the host for its own traffic is never picked up.

## Verification

- Unit tests (`tests/unit/llm/`) cover each mechanism. The adapter's tests run against a
  mock transport serving SSE streams, pinning the SDK's behaviour (paths, headers, error
  classes, mid-stream failures) at the locked version.
- Property-based contract tests (`tests/contract/test_llm_requests.py`) cover request
  shape, the frozen prefix, append-only growth, no prefill, and the fallback echo rules.
- `msa llm ping` is a two-turn tool round trip. The model must call a tool to learn a
  token it cannot guess. Its second request replays the first turn's thinking under
  `block_binding: error`, so a recorded 200 proves the transcript is append-only against
  the real API. Recorded once (`--record`), it replays offline in CI
  (`tests/contract/test_live_cassettes.py`), and replay fails on any byte of drift.

## Consequences

- Any change to a prompt, a tool, a schema, a beta or a model id invalidates the live
  cassettes. That is the point: each re-record proves the new request against the API.
  Re-recording needs a key, locally or via the `record cassettes` workflow.
- Retrying at two levels is bounded: at most `max_retries` SDK attempts per gateway
  attempt, and gateway attempts only for mid-stream failures.

## Open questions

- **[Unverified] Are errored or cut-off streams billed?** The gateway records no spend
  for them. If the API bills the partial output, spend is under-reported by at most that
  partial output, once per retried attempt. Check this against the console after the
  first live runs.
- **[Assumed] Cache rates of the fallback models.** The `msa.toml` cache prices for
  `claude-opus-5` and `claude-opus-4-8` assume the standard multipliers. Check them on the
  pricing page.
- Context-window stops (`model_context_window_exceeded`, `compaction`) fail the
  invocation for now. M6 decides compaction, once real transcripts show their sizes.

## Rejected

- **Retrying every retryable error in the gateway.** It multiplies with the SDK's
  retries (up to nine attempts at the defaults) and ignores `retry-after`.
- **`drop_block` mode.** It keeps runs alive by hiding the very bug the append-only
  transcript exists to prevent.
- **Pricing from top-level usage.** It under-reports every turn a fallback served.
- **Counting tokens before every turn.** That is an extra round trip on every turn,
  bought only where the budget binds.
- **The SDK's typed params at the port.** Cassettes, the store and the fake would all need
  SDK objects, and a digest over them would depend on SDK serialization details.
