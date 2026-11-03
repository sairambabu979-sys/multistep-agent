# ADR-006: Path ownership enforced as capabilities

- **Status:** Accepted (2026-11-02)

## Context

If the Coder can edit the Tester's tests, "tests pass" can be achieved by weakening the
tests. If repository content can talk an agent into new permissions, prompt injection
becomes privilege escalation.

## Decision

Each role has a write capability expressed as path globs (Coder: source; Tester:
`tests/acceptance/**`, `tests/repro/**`; Reviewer and Planner: none). The tool layer checks
it before every write and returns `CapabilityDenied` as a tool error. No prompt can change
it.

## Consequences

- The two BUILDING regions write disjoint paths, so they can run in separate worktrees
  and merge without conflicts by construction.
- Revisions are addressed to an owner (`ReviseChanges.owner`), which the state machine
  already carries (M1).
