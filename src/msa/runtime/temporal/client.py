"""Talking to Temporal: a worker for runs, and starting and answering them.

A run's workflow id is ``aegis-<run id>``, so anything that knows the run can reach it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from msa.core import RunState
from msa.core.events import AnyEvent
from msa.core.ids import Digest, RunId
from msa.core.values import Limits
from msa.runtime.codec import decode_state
from msa.runtime.journal import SqlJournal
from msa.runtime.temporal.activities import Activities, Factory
from msa.runtime.temporal.workflow import TASK_QUEUE, RunSpec, RunWorkflow, Start, encode

# The core and its models are imported once, outside the sandbox: they are pure (a test
# enforces it), and re-importing pydantic models per workflow would be slow and fragile.
PASSTHROUGH = ("msa", "pydantic", "pydantic_core", "annotated_types")


def workflow_id(run_id: str) -> str:
    return f"aegis-{run_id}"


def worker(
    client: Client,
    *,
    journal: Callable[[], SqlJournal],
    factory: Factory,
    task_queue: str = TASK_QUEUE,
) -> Worker:
    """A worker that hosts run workflows and executes their effects."""
    restrictions = SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[RunWorkflow],
        activities=Activities(journal, factory).all(),
        workflow_runner=SandboxedWorkflowRunner(restrictions=restrictions),
    )


async def start_run(
    client: Client,
    run_id: RunId,
    *,
    task_digest: Digest,
    limits: Limits,
    environment: dict[str, Any],
    clock: str = "system",
    snapshot_every: int = 50,
    heartbeat_seconds: int = 60,
    task_queue: str = TASK_QUEUE,
) -> WorkflowHandle[Any, str]:
    """Start a new run on Temporal. It is journaled as it goes, like any other."""
    spec = RunSpec(
        run_id=run_id,
        start=Start(task_digest, limits.model_dump_json(), json.dumps(environment)),
        clock=clock,
        snapshot_every=snapshot_every,
        heartbeat_seconds=heartbeat_seconds,
    )
    return await client.start_workflow(
        RunWorkflow.run, spec, id=workflow_id(run_id), task_queue=task_queue
    )


async def adopt_run(
    client: Client,
    run_id: RunId,
    *,
    clock: str = "system",
    snapshot_every: int = 50,
    task_queue: str = TASK_QUEUE,
) -> WorkflowHandle[Any, str]:
    """Drive a run that is already in the journal (started by ``msa run``, or a workflow
    that was terminated) from where its journal stands."""
    spec = RunSpec(run_id=run_id, clock=clock, snapshot_every=snapshot_every)
    return await client.start_workflow(
        RunWorkflow.run, spec, id=workflow_id(run_id), task_queue=task_queue
    )


async def send(client: Client, run_id: str, event: AnyEvent) -> None:
    """Deliver a person's answer, a pause or a cancel to a run's workflow. The workflow
    folds it through the core, which refuses it if the run is not waiting for it."""
    await client.get_workflow_handle(workflow_id(run_id)).signal(RunWorkflow.event, encode(event))


async def state_of(client: Client, run_id: str) -> RunState | None:
    """The workflow's current state, or None before its first step."""
    payload: str = await client.get_workflow_handle(workflow_id(run_id)).query(RunWorkflow.state)
    return decode_state(payload) if payload else None
