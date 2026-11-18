"""Whole live runs on the greeter fixture, with every executor real and the model scripted.

The model is scripted per role (``RoleScripts``): the Coder and the Tester build at the
same time, so their requests interleave in an order no single script could predict. Each
role reads its own script in order, whatever the interleaving.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from msa.config import MsaConfig
from msa.core import RunState
from msa.core.ids import RunId
from msa.llm import fake
from msa.llm.accounting import estimate_input_tokens
from msa.llm.client import Request, Response
from msa.llm.store import MemoryResponseStore
from msa.runtime import live
from msa.runtime.clock import LogicalClock
from msa.runtime.journal import SqliteJournal
from msa.tools.audit import Violation
from msa.tools.workspace import content_hash
from tests.support.llm import config as base_config
from tests.support.repos import make_repo

GOLDEN = Path(__file__).resolve().parents[1] / "integration" / "golden"
RUN = RunId("run-1")
TASK = 'Add farewell(name) to src/greeter.py, returning "goodbye <name>", with a test.'

BASE_GREETER = '"""Greetings."""\n\n\ndef greet(name: str) -> str:\n    return f"hello {name}"\n'
FAREWELL = '\n\ndef farewell(name: str) -> str:\n    return f"goodbye {name}"\n'
GOOD_GREETER = BASE_GREETER + FAREWELL
LINTY_GREETER = GOOD_GREETER.replace('"""Greetings."""\n', '"""Greetings."""\n\nimport os\n')
FAREWELL_TEST = (
    "from greeter import farewell\n\n\ndef test_farewell() -> None:\n"
    '    assert farewell("ada") == "goodbye ada"\n'
)
ACCEPTANCE = "tests/acceptance/test_goodbye.py"  # a basename no other test module has
ACCEPTANCE_TEST = (
    "from greeter import farewell\n\n\ndef test_farewell_says_goodbye() -> None:\n"
    '    assert farewell("ada") == "goodbye ada"\n'
)

PLAN = {
    "summary": "Add farewell next to greet, and a test for it.",
    "steps": [
        {"description": "Add farewell(name) to the greeter module.", "files": ["src/greeter.py"]},
        {"description": "Test farewell.", "files": ["tests/test_farewell.py"]},
    ],
    "acceptance_criteria": ['farewell("ada") returns "goodbye ada"'],
    "risks": [],
}
SUMMARY = {
    "summary": "Added farewell and its test.",
    "files_changed": ["src/greeter.py", "tests/test_farewell.py"],
    "checks_run": ["pytest: 2 passed"],
    "open_issues": [],
}
TESTS_SUMMARY = {
    "summary": "farewell returns goodbye and the name.",
    "tests": [f"{ACCEPTANCE}::test_farewell_says_goodbye"],
    "criteria_covered": ['farewell("ada") returns "goodbye ada"'],
    "open_issues": [],
}


# ---------- script builders


def tool_turn(*calls: dict[str, Any]) -> dict[str, Any]:
    return fake.message(fake.thinking(), *calls, stop_reason="tool_use")


def read(call_id: str, path: str) -> dict[str, Any]:
    return fake.tool_use(call_id, "read_file", {"path": path, "start_line": None, "end_line": None})


def edit(call_id: str, path: str, old: str, new: str, before: str) -> dict[str, Any]:
    arguments = {
        "path": path,
        "old": old,
        "new": new,
        "expected_hash": content_hash(before.encode()),
    }
    return fake.tool_use(call_id, "edit", arguments)


def create(call_id: str, path: str, content: str) -> dict[str, Any]:
    return fake.tool_use(call_id, "create_file", {"path": path, "content": content})


def planner_script() -> list[fake.Step]:
    return [tool_turn(read("p1", "src/greeter.py")), fake.answer(PLAN)]


def coder_script(new_greeter: str) -> list[fake.Step]:
    return [
        tool_turn(read("c1", "src/greeter.py")),
        tool_turn(
            edit("c2", "src/greeter.py", BASE_GREETER, new_greeter, BASE_GREETER),
            create("c3", "tests/test_farewell.py", FAREWELL_TEST),
        ),
        tool_turn(fake.tool_use("c4", "run", {"command": "pytest", "paths": [], "keyword": None})),
        fake.answer(SUMMARY),
    ]


def author_script(content: str = ACCEPTANCE_TEST, path: str = ACCEPTANCE) -> list[fake.Step]:
    return [tool_turn(create("t1", path, content)), fake.answer(TESTS_SUMMARY)]


def tool_results(request: Request) -> list[dict[str, Any]]:
    content = request["messages"][-1]["content"]
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


def prompt_of(request: Request) -> str:
    text = request["messages"][0]["content"]
    assert isinstance(text, str)
    return text


# ---------- the model, scripted per role


_ROLE = re.compile(r"# Role: (\w+)")


class RoleScripts:
    """A ``MessagesClient`` with one script per role, chosen by the system prompt."""

    def __init__(self, **scripts: Iterable[fake.Step]) -> None:
        self.clients = {role: fake.ScriptedClient(*steps) for role, steps in scripts.items()}

    @staticmethod
    def role_of(request: Request) -> str:
        found = _ROLE.match(request["system"][0]["text"])
        assert found is not None, "every role prompt starts with '# Role: <name>'"
        return found[1].lower()

    async def create(self, request: Request) -> Response:
        role = self.role_of(request)
        if role not in self.clients:
            raise AssertionError(f"no script for the {role}")
        return await self.clients[role].create(request)

    async def count_tokens(self, request: Request) -> int:
        return estimate_input_tokens(request)

    def requests(self, role: str) -> list[Request]:
        return self.clients[role].requests

    @property
    def exhausted(self) -> bool:
        return all(c.exhausted for c in self.clients.values())


# ---------- the harness


@dataclass
class Harness:
    repo: Path
    state_dir: Path
    config: MsaConfig
    violations: list[Violation] = field(default_factory=list)

    def run(self, client: RoleScripts) -> tuple[RunState, SqliteJournal]:
        environment = live.LiveEnvironment(
            repo=str(self.repo), state_dir=str(self.state_dir), task=TASK
        )
        journal = SqliteJournal(self.state_dir / "runs.db")
        interpreter = live.interpreter(
            environment,
            self.config,
            run_id=RUN,
            journal=journal,
            client=client,
            store=MemoryResponseStore(),
            clock=LogicalClock(),
            on_violation=self.violations.append,
        )
        final = asyncio.run(
            interpreter.start(
                run_id=RUN,
                task_digest=environment.task_digest,
                limits=self.config.limits.to_limits(),
                environment=environment.model_dump(mode="json"),
            )
        )
        return final, journal

    @property
    def run_dir(self) -> Path:
        return self.state_dir / "runs" / RUN

    def report(self) -> str:
        return (self.run_dir / "report.md").read_text(encoding="utf-8")

    def artifact(self, digest: str) -> str:
        return (self.run_dir / "artifacts" / digest.removeprefix("sha256:")).read_text(
            encoding="utf-8"
        )


def make_harness(base: Path) -> Harness:
    """The greeter fixture, committed, with ruff and pytest from this interpreter."""
    config = base_config()
    config = config.model_copy(
        update={
            "sandbox": config.sandbox.model_copy(update={"launcher": (sys.executable, "-m")}),
            "verify": config.verify.model_copy(
                update={"commands": ("ruff_check", "ruff_format_check", "pytest")}
            ),
        }
    )
    return Harness(make_repo("greeter", base / "repo"), base / "state", config)


# ---------- golden timelines

# The Coder and the Tester build concurrently, so which one finishes first varies. Whichever
# is second issues integrate_build; the golden file records the regions, not their order.
_REGION = re.compile(r"BUILDING -> BUILDING  (ImplementationCompleted|AcceptanceTestsAuthored)")


def normalized(lines: list[str]) -> str:
    return "\n".join(_REGION.sub("BUILDING -> BUILDING  <region done>", line) for line in lines)


def check_golden(name: str, lines: list[str]) -> None:
    path = GOLDEN / name
    if os.environ.get("MSA_UPDATE_GOLDEN") == "1":
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    expected = path.read_text(encoding="utf-8").splitlines()
    assert normalized(lines) == normalized(expected)


def phases(lines: list[str]) -> list[str]:
    """``SOURCE -> TARGET`` for each timeline line."""
    return [line.strip().split("  ")[1] for line in lines]
