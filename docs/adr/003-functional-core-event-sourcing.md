# ADR-003: Functional core with an event-sourced journal

- **Status:** Accepted (2026-11-02)

## Context

A multistep agent run is long, expensive, and non-deterministic at its edges (LLMs,
subprocesses). Crashes must not lose paid-for work, and every decision must be auditable.

## Decision

- The orchestrator is a pure function `transition(state, event) -> (state, effects)` in
  `msa.core`. It performs no I/O and reads no clock or randomness. An AST allowlist test
  enforces this.
- Every accepted event is appended to a journal (M2). State is `fold(transition, events)`.
- Effects carry deterministic ids (`run:seq:index`) so the interpreter can deduplicate on
  resume.
- Invariants are checked on *every* transition in production. A violation raises at the
  step that caused it.
- The transition table is data. Each rule declares the phases it may move to, the
  dispatcher enforces that, and the diagram, the static analysis, and the exhaustive model
  checker are all derived from it.

## Consequences

- The entire orchestration logic is testable without an LLM. M1 model-checks every
  reachable state at the default limits (about 81k states) in CI.
- Events need schema versions and upcasters once the journal persists (M2).

## Rejected

- **Mutable orchestrator object with callbacks:** simpler to write, impossible to replay
  or model-check.
- **A workflow engine (Temporal etc.):** durable, but heavy, and it hides the state space
  we want to verify.
