# ADR-022: Bound what tool results add to the transcript

- **Status:** Accepted (2026-11-27)
- **Refines:** ADR-008 (the append-only transcript), ADR-009 (the workspace tools)

## Context

Every tool result stays in an invocation's transcript, and every later turn sends it again.
A `read_file` could return up to 100,000 characters, and a larger range was refused with an
error that said only "read it in ranges". A Coder or Tester invocation runs up to 60 turns,
so the transcript could grow to hundreds of thousands of tokens, most of it files read
early and never needed again.

The client may not trim old results itself. The transcript is append-only by type: that
keeps the cached prefix byte-stable and passes the API's history-editing check on thinking
blocks (`block_binding.prefix_mismatch_behavior: "error"`). Results have to be bounded
before they are first sent, or cleared by the server.

## Decision

1. **A 32,000-character view, cut with a next range** (`[sandbox].view_limit_chars`,
   default 32,000, was 100,000).
   - A range over the limit is no longer refused. The view returns the leading lines that
     fit, and ends with
     `[cut at the 32000-character view limit: lines N-M not shown. Read on with start_line=N.]`.
   - Following that hint from view to view returns every line once, in order.
   - A single line longer than the limit is still refused, with a pointer to `search`.
2. **Server-side context management** (`[api.context_management]`), on invocations with
   tools.
   - Every such request sends a `clear_tool_uses_20250919` edit under beta
     `context-management-2025-06-27`, with three thresholds:
     - `trigger_input_tokens`: 200,000;
     - `keep_tool_uses`: 8, the newest tool uses kept whole;
     - `clear_at_least_input_tokens`: 60,000.
   - `enabled = false` turns it off.
   - What the server cleared (`context_management.applied_edits`) is recorded on each
     `Turn` as `cleared_tool_uses` and `cleared_input_tokens`.
3. **The client's side does not change.** The gateway still sends its whole append-only
   transcript; the server clears what the model sees. So:
   - **Thinking blocks:** the history-editing check runs on the request as sent, so a
     server-side clear is not an edit.
   - **The journal and replay:** a request's digest depends only on the transcript and
     `msa.toml`, so a resumed run sends byte-identical requests and replays paid turns from
     the response store, cleared counts included.
   - **Budgets:** `count_tokens` receives the same `context_management`, so a tight budget
     is planned on the prompt the model will actually see.

## Consequences

- **The thresholds are high on purpose: clearing usually costs more than it saves.**
  - **The cost:** a clear invalidates the cached conversation from the first result it
    removes. The next request writes the rest again, at the write price ($8 per million
    tokens for a 1-hour write on Claude Opus 5.5, ADR-021).
  - **The saving:** only the cleared tokens, at the read price ($0.20 per million).
  - **Example:** clearing 30,000 tokens of a 100,000-token prompt rewrites about 65,000.
    That costs about $0.52 once and saves about $0.006 a turn, so it breaks even after
    about 87 turns. The Coder's cap is 60.

  At these thresholds a clear happens only on an invocation that has grown very large. It
  is a guard against unbounded growth, not a cost saving. The view limit is the saving:
  each read adds at most a third of what it could before.
- **Turns record what was cleared**, so whether the guard ever fires is visible in a live
  run's metrics before anyone tunes it.
- **A cleared `read_file` loses its `content_hash`.** An `edit` needs that hash, so the
  model reads the file again. The newest eight tool uses are never cleared.
- **Tests on the fake model:**
  - every request with tools carries the edit and the beta, and requests without tools
    carry neither;
  - it can be switched off;
  - cleared counts are recorded, and survive a replay;
  - the transcript still only grows after the server reports a clear;
  - the exact count uses the same edit;
  - cut views carry the next range, and following it covers a file exactly once.

## Rejected

- **Trimming old tool results in the client.** It would break the cached prefix and fail
  the history-editing check on every later turn.
- **Compaction (`compact_20260112`).** It summarizes rather than clears. Its interaction
  with preserved thinking needs its own design, and invocations are capped well below the
  context window.
- **A low trigger, to save money.** At current prices a small clear is a net cost (above).
