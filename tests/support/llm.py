"""Shared builders for LLM-layer tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from msa.config import MsaConfig, Role, load_config
from msa.llm.gateway import Invocation, Tool, ToolOutput

ROOT = Path(__file__).resolve().parents[2]


def config(**api_overrides: Any) -> MsaConfig:
    base = load_config(ROOT / "msa.toml")
    if not api_overrides:
        return base
    return base.model_copy(update={"api": base.api.model_copy(update=api_overrides)})


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "not ok"]
    notes: str


class FilePath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


async def read_file(arguments: FilePath) -> ToolOutput:
    return ToolOutput(f"contents of {arguments.path}")


READ = Tool("read_file", "Read a file.", FilePath, read_file, parallel_safe=True)


def invocation(**overrides: Any) -> Invocation[Verdict]:
    values: dict[str, Any] = {
        "role": Role.REVIEWER,
        "system": "You review code.",
        "prompt": "Review the change.",
        "output": Verdict,
        "budget_usd_micros": 2_000_000,
        "idempotency_key": "run-1:9:0",
        "tools": (READ,),
    }
    values.update(overrides)
    return Invocation(**values)


VERDICT = {"status": "ok", "notes": "fine"}
