# ADR-004: Settle correctness disputes by running code

- **Status:** Accepted (2026-11-02)

## Context

When an author LLM and a reviewer LLM negotiate, the conversation tends to fall into one
of three patterns: the author accepts everything, the reviewer approves everything, or new
objections keep appearing every round.

## Decision

Disputes are resolved by the cheapest objective arbiter available (PLAN §4.2):

1. **Correctness or security:** the Tester writes a repro test, and its result is the verdict.
2. **Preferences:** the preference store decides.
3. **Uncovered style:** the author wins. Taste never blocks a merge.
4. **Remaining blocking disputes:** escalate to a human (default) or the Arbiter role.

The state machine enforces the structural parts: bounded rounds, strict progress per
round, exhaustive rulings, and failing fast once a demanded change can no longer be made.

## Consequences

- Repro tests become regression tests, so disputes leave the codebase better tested.
- The negotiation's value must be proven by the ablation in PLAN §11, or it ships disabled.
