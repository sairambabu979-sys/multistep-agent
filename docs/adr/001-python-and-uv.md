# ADR-001: Python 3.12+ with uv

- **Status:** Accepted (2026-11-02)

## Context

The system's riskiest component is a state machine whose correctness must be shown, not
asserted (PLAN §2.4). The language choice is mostly a choice of verification tooling.

## Decision

Python 3.12+, managed with `uv` (`uv.lock` committed, CI installs with `--locked`).

- **Hypothesis** gives property-based *and* stateful (`RuleBasedStateMachine`) testing,
  with no equivalent of comparable maturity in TypeScript.
- **Pydantic v2** gives frozen, discriminated-union value objects that serialize to the
  journal without hand-written codecs.
- PEP 695 generics and `StrEnum` keep the core typed under `mypy --strict`.

## Consequences

- `mypy --strict` over `src` *and* `tests` is mandatory. Untyped code does not merge.
- The Anthropic Python SDK is the integration point from M3 onward.

## Rejected

- **TypeScript:** a strong type system and Zod, but weaker stateful property testing.
