# ADR-002: Own the agent loop on the Messages API

- **Status:** Accepted (2026-11-02)

## Context

Each agent role runs a tool-use loop. The orchestrator must checkpoint every turn to the
journal (resume without paying twice), enforce per-role capabilities, attribute cost per
role, and replay recorded runs offline (cassettes).

## Decision

Write the loop ourselves on the Messages API (`messages.stream` + tool results), behind a
`gateway` port in `msa.runtime`. Only `msa/runtime/gateway.py` imports `anthropic`.

## Consequences

- We own retries, refusal handling, prompt-cache hygiene, and append-only transcripts.
  These are the details M3 tests explicitly.
- More code than a managed harness, but every turn is visible to the journal and to tests.

## Rejected

- **SDK tool runner:** convenient, but it hides the per-turn boundary we need to journal.
- **Claude Agent SDK:** batteries-included coding tools. It is a credible alternative
  *adapter* for the Coder role later, behind the same port, but it cannot be the core.
  The state machine and the negotiation protocol are the product.
- **Managed Agents:** hosts the loop and the sandbox, but moves the orchestration we
  want to own (and test exhaustively) off our machine.
