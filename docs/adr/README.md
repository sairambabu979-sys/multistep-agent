# Architecture Decision Records

Each record captures one decision: the context, the choice, what it costs, and what was
rejected. Records are append-only. A reversed decision gets a new record that supersedes
the old one.

| # | Decision | Status |
|---|---|---|
| [001](001-python-and-uv.md) | Python 3.12+ with uv | Accepted |
| [002](002-own-the-agent-loop.md) | Own the agent loop on the Messages API | Accepted |
| [003](003-functional-core-event-sourcing.md) | Functional core with an event-sourced journal | Accepted |
| [004](004-executable-arbitration.md) | Settle correctness disputes by running code | Accepted, refined by 014 |
| [005](005-single-model-per-role-effort.md) | One model, effort tuned per role | Accepted |
| [006](006-path-ownership-capabilities.md) | Path ownership enforced as capabilities | Accepted |
| [007](007-commit-point-and-crash-semantics.md) | The journal append is the commit point; effects are at-least-once | Accepted |
| [008](008-llm-gateway.md) | The LLM gateway: append-only requests, a hard budget, replayable turns | Accepted |
| [009](009-tool-layer-and-local-sandbox.md) | The tool layer: race-free confinement, capabilities on resolved paths, an honest local sandbox | Accepted |
| [010](010-preferences.md) | Preferences: field-level layers, sound conflict detection, checks before tokens | Accepted |
| [011](011-first-slice-worktree-and-stand-ins.md) | The first slice: one run worktree, harness-made trees, honest stand-ins | Accepted |
| [012](012-tester-and-evidence.md) | The Tester: blind tests, evidence the harness decides, an owner for every failure | Accepted, refined by 013 |
| [013](013-evidence-integrity.md) | Evidence integrity: tests that ran, configuration the Coder cannot touch, checks on the delivered tree | Accepted |
| [014](014-review-negotiation-arbitration.md) | Review, negotiation and arbitration: scope by lines, assent by the other side, the ladder before any person | Accepted |
| [015](015-aegis-architecture.md) | AEGIS: eight roles, a twenty-state workflow, and the stack around a pure core | Accepted |
| [016](016-skills-and-security-policy.md) | Skills and the security policy: versioned, narrowing, and outranked by policy | Accepted |
| [017](017-postgres-and-temporal.md) | PostgreSQL storage and Temporal orchestration, around the same core | Accepted |
| [018](018-push-and-pull-requests.md) | After approval, push the release and open its pull request, if asked | Accepted |
| [019](019-container-sandbox.md) | A container backend for repository commands, for repositories you do not trust | Accepted |
| [020](020-dependency-audit.md) | Audit the pinned Python dependencies a change adds, by default | Accepted |
| [021](021-prompt-cache-ttl.md) | A one-hour prompt cache, configurable | Accepted |
