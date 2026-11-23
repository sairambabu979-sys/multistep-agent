# ADR-007: The journal append is the commit point; effects are at-least-once

- **Status:** Accepted (2026-11-03)

## Context

Agent runs are long and expensive. A process can die at any instruction: an OOM kill,
a deploy, a laptop lid. "Exactly-once" execution of effects is not achievable across
a crash: an effect can finish its work an instant before the process dies, before anything
durable records that it finished.

## Decision

1. **One commit point.** For every event, the interpreter computes the transition (pure,
   so free to discard), *then* appends the event to the journal, *then* launches the new
   effects. The append is a compare-and-set on the run's head sequence.
2. **Journaled completions are final.** On resume, an effect whose completion is in the
   journal is not pending, so it is never executed again. This is the guarantee that
   matters for cost: a completed LLM call is never paid for twice.
3. **In-flight effects are at-least-once.** An effect that was running at the crash is
   executed again. Every executor receives the deterministic effect id
   (`<run>:<seq>:<index>`) as an idempotency key. The LLM gateway (M3) uses it to return a
   cached response for a repeated call.
4. **One writer per run.** A second writer (`msa cancel`, or a second interpreter) loses
   the compare-and-set. The interpreter then adopts the journal's history instead of
   overwriting it.
5. **Executor failures are events.** An exception, an answer for the wrong effect, or an
   answer the core rejects becomes `AgentFailed`/`EffectFailed`. The run fails with a
   reason; the interpreter itself never crashes on an executor bug.

## Verification

The chaos test (`tests/chaos/test_crash_recovery.py`) forks a run and has the child
`SIGKILL` itself at random points:
- an effect starting;
- a transition computed but not yet durable;
- an event durable but its effects not yet launched.

Each run is crashed up to three times. Every trial must reach exactly the state of an
uninterrupted run with the same seed, and no journaled effect may execute again.
A deliberate determinism break makes all trials fail, so the test can detect divergence.

## Consequences

- Executors must be idempotent per effect id, or at least safe to repeat. This is part of
  the executor contract from M3 on.
- Throughput is bounded by one fsync per event (`synchronous=FULL`). Agent runs produce
  tens to hundreds of events, so this is negligible next to LLM latency.

## Rejected

- **Write-ahead "effect started" records:** they would not give exactly-once either.
  A crash between the record and the call still leaves an ambiguous effect. They would
  double the writes.
- **Two-phase commit with executors:** LLM APIs and subprocesses cannot take part in it.
