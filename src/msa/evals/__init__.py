"""The eval harness (PLAN §11): proving each part of the system earns its cost.

- ``corpus``: the hand-built tasks, their base repositories, hidden tests and references.
- ``scoring``: the gate, the visible tests and the hidden tests on a tree, in the sandbox.
- ``validity``: whether every task is fit to score (``msa eval check``).
- ``runner``: one live run per (task, arm), scored by hidden tests (``msa eval run``).
- ``report``: the metrics per arm and what the registered rule decides (``msa eval report``).
"""
