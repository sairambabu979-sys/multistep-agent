# ADR-005: One model, effort tuned per role

- **Status:** Accepted (2026-11-02)

## Context

Different roles need different depth (an Arbiter ruling vs. a triage classification).
Mixing models per role looks cheaper on paper.

## Decision

v1 uses one model for every role and varies only `output_config.effort` per role. All
model ids, beta flags, and prices live in one config file.

## Consequences

- One prompt-cache namespace (caches are model-scoped), so cache reuse holds across roles.
- A cheaper model per role is adopted only when the eval (M9) shows quality holds for that
  role. That decision is measured, never assumed.
