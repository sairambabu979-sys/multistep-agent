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
