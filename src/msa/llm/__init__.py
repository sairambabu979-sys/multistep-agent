"""The LLM layer: one agent invocation as a bounded, budgeted, replayable tool loop.

Agents (M6-M8) build on this; the Anthropic SDK is imported in exactly one module,
``msa.llm.anthropic_client``. See ADR-008.
"""
