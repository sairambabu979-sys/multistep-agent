# ADR-021: A one-hour prompt cache, configurable

- **Status:** Accepted (2026-11-27)
- **Refines:** ADR-008 (the gateway and its cached prefix)

## Context

Every request the gateway sends sets two cache breakpoints: one on the role's system prompt,
and the request-level breakpoint that follows the growing conversation. Both used the
default five-minute TTL.

Five minutes is shorter than gaps that happen in every run:
- a `run` tool call can take up to `[sandbox].timeout_seconds` (600 s);
- the gates between two invocations of a role (static analysis, unit and integration
  tests) often take longer;
- a human gate can take any length of time.

After such a gap the next request writes its whole prefix again, at the write price.

## Decision

1. **`[api].cache_ttl`, `"1h"` or `"5m"`, default `"1h"`.** It sets the TTL of both
   breakpoints, sent as `{"type": "ephemeral", "ttl": ...}`.
2. **One TTL for both breakpoints.** The API requires a longer-lived entry to come before a
   shorter one. A 1-hour conversation breakpoint after a 5-minute system breakpoint is a
   400. With a single setting that cannot be configured by mistake.
3. **Unlabelled writes are priced at that TTL.** `spend_of` prices cache writes from
   `usage.cache_creation`, which splits them into 5-minute and 1-hour tokens. Without that
   breakdown it used to price every write at the 5-minute rate. That was right only while
   the gateway wrote nothing else. It now uses `[api].cache_ttl`'s rate, so a 1-hour write
   is never under-reported, by 37.5% at the configured rates ($5 against $8 per million on
   Claude Opus 5.5). The budget reservation already priced every input token at its worst
   rate, which includes the 1-hour write rate, so no reservation changes.

## Consequences

- **A write costs 2× input instead of 1.25×, and a read costs the same.** This is the
  trade-off, and it is not free:
  - **The system prompt** is written once per role and read by every later invocation of
    that role. It gains whenever the gap between invocations exceeds five minutes, which
    is usual.
  - **The conversation breakpoint** writes each turn's new tail. Inside one invocation,
    turns are usually seconds apart, so a 5-minute entry would stay warm anyway, and each
    of those incremental writes now costs 60% more. It pays off only across gaps longer
    than five minutes: a long `run` call, or a run resumed after a crash.

  The split that might cost least, a 1-hour system breakpoint with a 5-minute conversation
  breakpoint, is allowed by the API. It is not offered, because the only evidence to choose
  between them is a paid comparison. `cache_ttl = "5m"` restores the old behaviour.
- **Requests change shape.** A response store recorded before this change does not match
  the new requests, so a run in flight across an upgrade pays for its next turn instead of
  replaying it. No recorded cassette exists yet.
- **Tests:**
  - every request's two breakpoints carry the configured TTL (the request contract and a
    gateway test, for both values);
  - unlabelled writes are priced at the configured TTL, and a breakdown wins over it;
  - a TTL other than `"5m"` or `"1h"` is refused when `msa.toml` loads.

## Rejected

- **Leaving five minutes.** Every gap longer than five minutes paid for a full rewrite.
- **Separate TTLs per breakpoint.** That gives more ways to send a request the API rejects,
  for a saving no one has measured.
