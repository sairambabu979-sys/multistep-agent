"""The service's side of Temporal (ADR-017).

With ``[temporal].address`` set, runs started from the API are started on Temporal, and
``msa worker`` drives them. An answer to a run that a workflow drives is validated by the
core exactly as before, then reaches the workflow as a signal instead of being journaled
here: the workflow journals it. The service never drives such a run itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from msa.config import TemporalConfig
from msa.core.events import AnyEvent
from msa.core.ids import Digest, RunId
from msa.core.values import Limits

if TYPE_CHECKING:
    from temporalio.client import Client


def _client_module() -> Any:
    """msa.runtime.temporal.client, imported only when Temporal is configured: temporalio
    is an optional dependency."""
    from msa.runtime.temporal import client  # noqa: PLC0415

    return client


def _clock(environment: dict[str, Any]) -> str:
    return "logical" if environment.get("kind") == "simulation" else "system"


class TemporalBridge:
    def __init__(self, config: TemporalConfig, *, snapshot_every: int = 50) -> None:
        if config.address is None:
            raise ValueError("a Temporal bridge needs [temporal].address")
        self.config = config
        self.snapshot_every = snapshot_every
        self.driven: set[RunId] = set()  # runs a workflow was last seen driving
        self._client: Client | None = None

    async def client(self) -> Client:
        if self._client is None:
            assert self.config.address is not None
            self._client = await _client_module().connect(
                self.config.address, self.config.namespace
            )
        return self._client

    async def driving(self, run_id: RunId) -> bool:
        """Whether a workflow drives ``run_id`` now, remembered for listings."""
        running = bool(await _client_module().driving(await self.client(), run_id))
        if running:
            self.driven.add(run_id)
        else:
            self.driven.discard(run_id)
        return running

    async def start(
        self,
        run_id: RunId,
        *,
        task_digest: Digest,
        limits: Limits,
        environment: dict[str, Any],
    ) -> None:
        await _client_module().start_run(
            await self.client(),
            run_id,
            task_digest=task_digest,
            limits=limits,
            environment=environment,
            clock=_clock(environment),
            snapshot_every=self.snapshot_every,
            task_queue=self.config.task_queue,
        )
        self.driven.add(run_id)

    async def adopt(self, run_id: RunId, environment: dict[str, Any]) -> None:
        """Hand a journaled run that nothing drives to a workflow."""
        await _client_module().adopt_run(
            await self.client(),
            run_id,
            clock=_clock(environment),
            snapshot_every=self.snapshot_every,
            task_queue=self.config.task_queue,
        )
        self.driven.add(run_id)

    async def send(self, run_id: RunId, event: AnyEvent) -> None:
        await _client_module().send(await self.client(), run_id, event)
