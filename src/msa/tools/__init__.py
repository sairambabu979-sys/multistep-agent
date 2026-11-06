"""The capability-checked tool layer agents act through (PLAN §3.2, ADR-009).

- ``paths``: confine a path to the worktree, then open it without following symlinks.
- ``capabilities``: which role may write which paths (ADR-006).
- ``workspace``: read_file, create_file and edit for one role.
- ``sandbox``: allowlisted commands, search and git_diff.
- ``audit``: security refusals, recorded for the operator.

It depends only on ``msa.config`` and ``msa.core``. ``msa.agents.toolset`` binds it to the
LLM gateway.
"""
