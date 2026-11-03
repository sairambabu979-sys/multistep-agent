# multistep-agent

A multistep coding agent built around a pure, event-sourced state machine:
planner → coder ∥ blind tester → deterministic verification → reviewer ⇄ author
negotiation with executable arbitration → finalize, all shaped by a layered
preferences skill.

**Status:** M0 (skeleton) and M1 (the state machine) are done. Agents arrive in M6–M8.
The full design is in [`docs/PLAN.md`](docs/PLAN.md), decisions are in [`docs/adr/`](docs/adr/),
and the generated state diagram is in [`docs/fsm.md`](docs/fsm.md).

## What exists today

The orchestrator core (`src/msa/core`) is a pure function:

```python
step = start(RunStarted(...))         # -> Step(state, effects, edge)
step = transition(step.state, event)  # raises TransitionRejected for stale/duplicate/illegal events
state = replay(journal)               # fold the journal back into the exact same state
```

It performs no I/O and reads no clock or randomness, which a test enforces. Every transition:

- rejects events for effects that aren't pending, so delivery is idempotent;
- binds evidence to the git tree it checked, so stale test results can't finalize a change;
- funds LLM effects from disjoint budget reservations, so parallel work can't overspend;
- runs all invariants in production, not only in tests.

## How it's verified

| Layer | What it shows |
|---|---|
| Exhaustive model check | Every reachable state at the default limits (about 81k): none stuck, all can still reach DONE, every declared edge taken |
| Hypothesis stateful | Random interleavings, including overspend, deadlines, cancellation, redelivered and forged events. The journal replays exactly and survives a JSON round trip. |
| Property tests | Every run terminates and publishes exactly one outcome. Every journal prefix replays. |
| Static analysis | Reachability and liveness over the declared transition graph (`msa fsm check`) |
| Contracts | Core purity (AST allowlist), schema completeness, `docs/fsm.md` freshness, layering (`import-linter`) |

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
make check      # lint, mypy --strict, architecture contracts, FSM analysis, tests, coverage gates
```

```bash
make nightly    # 10,000 Hypothesis examples per property
```

```bash
uv run msa fsm check
```

```bash
uv run msa fsm diagram --output docs/fsm.md
```
