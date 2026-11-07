"""Agent roles (PLAN §3): what each one is asked, with which tools, for which typed answer.

A role here is a function from its inputs to an ``msa.llm.gateway.Invocation``; the loop
itself is the gateway's. The Planner and Coder arrive in M6, the Tester, Reviewer and
Arbiter in M7-M8. Executors in ``msa.runtime`` gather the inputs and run the invocations.
"""
