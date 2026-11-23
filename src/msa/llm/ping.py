"""The smallest invocation that exercises everything the gateway promises, for cents.

Two turns: the model must call a tool to learn a token it cannot guess, then report it in
the structured answer. One successful ping proves, against the real API:

- the key, the base URL, the model id and every beta in ``msa.toml`` are accepted;
- strict tools and the strict answer schema are accepted, and the tool round trip works;
- the second request replays the first turn's thinking under ``block_binding: error``,
  so a 200 proves the transcript really is append-only;
- the response prices against the table (the served model and its spend are reported).

Recorded with ``msa llm ping --record``, it is also the cassette that keeps all of the
above checked offline in CI.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from msa.config import Effort, MsaConfig, Role
from msa.llm.client import MessagesClient
from msa.llm.gateway import Gateway, Invocation, InvocationResult, Tool, ToolOutput

TOKEN = "tangerine-4417"
BUDGET_USD_MICROS = 100_000  # a cap, not the cost: a ping spends well under a cent

SYSTEM = (
    "You are the health check of a coding agent. Use the tool you are given, then answer "
    "in exactly the format requested. Do not explain."
)
PROMPT = (
    'Call health_token with purpose set to "ping", then answer with the token it returns, '
    "exactly as returned."
)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(description='Why the token is needed; use "ping".')


class Pong(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(description="The token health_token returned, verbatim.")


async def _health_token(_: TokenRequest) -> ToolOutput:
    return ToolOutput(TOKEN)


HEALTH_TOKEN = Tool(
    "health_token",
    "Returns the token this health check must report.",
    TokenRequest,
    _health_token,
    parallel_safe=True,
)


def invocation() -> Invocation[Pong]:
    return Invocation(
        role=Role.PLANNER,
        system=SYSTEM,
        prompt=PROMPT,
        output=Pong,
        budget_usd_micros=BUDGET_USD_MICROS,
        idempotency_key="ping",
        tools=(HEALTH_TOKEN,),
        effort=Effort.LOW,
    )


@dataclass(frozen=True, slots=True)
class PingReport:
    result: InvocationResult[Pong]

    @property
    def used_the_tool(self) -> bool:
        return any(t.stop_reason == "tool_use" for t in self.result.turns)

    @property
    def ok(self) -> bool:
        """The token can only be known by calling the tool and reading its result."""
        return self.used_the_tool and self.result.output.token == TOKEN

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys(t.model for t in self.result.turns))


async def ping(client: MessagesClient, config: MsaConfig) -> PingReport:
    return PingReport(await Gateway(client, config).invoke(invocation()))
