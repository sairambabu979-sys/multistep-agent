"""Temporal orchestration (ADR-017): the pure core, driven by a durable Temporal workflow.

- ``workflow``: ``RunWorkflow`` folds events through the core and starts each pending
  effect as an activity. People's answers, pause and cancel arrive as signals. It is
  deterministic because the core is pure, and every time it reads is Temporal's.
- ``activities``: what touches the world. Executing an effect, through the same executors
  and failure rules as the in-process interpreter; and the journal, which stays the
  system of record, written idempotently.
- ``client``: starting runs, signalling them, and building a worker.

The in-process interpreter (``msa.runtime.interpreter``) remains the default driver;
this one adds what Temporal is for: runs that outlive any one process, activities
retried on another worker when one dies, and timers held by the server.
"""
