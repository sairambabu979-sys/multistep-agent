# Architecture Decision Records

Each record captures one decision: the context, the choice, what it costs, and what was
rejected. Records are append-only. A reversed decision gets a new record that supersedes
the old one.

| # | Decision | Status |
|---|---|---|
| [001](001-python-and-uv.md) | Python 3.12+ with uv | Accepted |
| [002](002-own-the-agent-loop.md) | Own the agent loop on the Messages API | Accepted |
| [003](003-functional-core-event-sourcing.md) | Functional core with an event-sourced journal | Accepted |
| [004](004-executable-arbitration.md) | Settle correctness disputes by running code | Accepted |
| [005](005-single-model-per-role-effort.md) | One model, effort tuned per role | Accepted |
| [006](006-path-ownership-capabilities.md) | Path ownership enforced as capabilities | Accepted |
| [007](007-commit-point-and-crash-semantics.md) | The journal append is the commit point; effects are at-least-once | Accepted |
| [008](008-llm-gateway.md) | The LLM gateway: append-only requests, a hard budget, replayable turns | Accepted |
| [009](009-tool-layer-and-local-sandbox.md) | The tool layer: race-free confinement, capabilities on resolved paths, an honest local sandbox | Accepted |
| [010](010-preferences.md) | Preferences: field-level layers, sound conflict detection, checks before tokens | Accepted |
| [011](011-first-slice-worktree-and-stand-ins.md) | The first slice: one run worktree, harness-made trees, honest stand-ins | Accepted |
| [011](011-first-slice-worktree-and-stand-ins.md) | The first slice: one run worktree, harness-made trees, honest stand-ins | Accepted |
