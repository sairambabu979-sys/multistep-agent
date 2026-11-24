"""Run ``msa`` with one bug planted in the Coder's first implementation; the rest is real.

Used by the `live run` workflow's ``seeded`` mode (.github/workflows/live-run.yml), on the
``restock`` task:

    uv run python scripts/live_run_seeded.py -- --config msa.toml run --repo ... \\
        --task-file .github/live-run/restock.md ...

Four live runs never raised a review finding: the real Coder's code kept passing review, so
neither the Reviewer catching a defect nor the negotiation that follows had run with the
real model. This plants one known defect where the scripted definition-of-done demo plants
it, and lets the real roles find it.

- **What is seeded:** the Coder's first invocation only (``implement_plan``). It is
  answered from the script below instead of the model, and writes a correct
  ``Store.restock``, the export and passing unit tests, plus ``receive()`` with the planted
  bug: it applies a delivery note line by line, so a bad line halfway through leaves the
  store partly restocked. It passes lint, types, the security rules and every behaviour the
  task names.
- **What is real:** every request of every other role (Planner, Tester, Reviewer, Arbiter,
  triage), and every later Coder request (revisions, answers to findings), go to the model
  through the client msa.toml configures. Whether anything catches the bug, and how, is
  the result.

The seeded turns carry placeholder thinking signatures, so a seeded conversation is never
sent to the API. Their token counts are estimated locally.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from msa import cli
from msa.config import MsaConfig
from msa.llm import fake
from msa.llm.accounting import estimate_input_tokens
from msa.llm.client import MessagesClient, Request, Response
from msa.runtime.live import LiveEnvironment
from msa.tools.workspace import content_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "repos" / "inventory"
STORE = "src/inventory/store.py"
RECEIVING = "src/inventory/receiving.py"
INIT = "src/inventory/__init__.py"
UNIT_TEST = "tests/test_restock.py"

RESTOCK = """
    def restock(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("a restock must add a positive quantity")
        item = self._items[sku]
        self._items[sku] = Item(sku, item.quantity + quantity)
"""
# The planted bug: each line is applied as it is read, so a bad line part-way through leaves
# the lines before it applied and the rest not.
RECEIVING_SEEDED = '''"""Deliveries: a note of "sku,quantity" lines, applied to a store."""

from inventory.store import Store


def receive(store: Store, note: str) -> int:
    """Restock ``store`` from ``note``; returns the number of lines applied."""
    applied = 0
    for line in note.splitlines():
        if not line.strip():
            continue
        sku, quantity = line.split(",")
        store.restock(sku.strip(), int(quantity))
        applied += 1
    return applied
'''
UNIT_TESTS = """from inventory import Store, receive


def test_restock_adds_to_the_stock() -> None:
    store = Store()
    store.add("apple", 3)
    store.restock("apple", 2)
    assert store.quantity("apple") == 5


def test_receive_applies_each_line() -> None:
    store = Store()
    store.add("apple", 1)
    store.add("pear", 0)
    assert receive(store, "apple,2\\npear,5\\n") == 2
    assert [(i.sku, i.quantity) for i in store.items()] == [("apple", 3), ("pear", 5)]
"""
SUMMARY = {
    "summary": "Added Store.restock, receive() in src/inventory/receiving.py, the export, "
    "and unit tests for both.",
    "files_changed": [STORE, RECEIVING, INIT, UNIT_TEST],
    "checks_run": ["pytest"],
    "open_issues": [],
}


def _turn(*calls: dict[str, Any]) -> Response:
    return fake.message(fake.thinking(), *calls, stop_reason="tool_use")


def _edit(call_id: str, path: str, old: str, new: str, before: str) -> dict[str, Any]:
    arguments = {
        "path": path,
        "old": old,
        "new": new,
        "expected_hash": content_hash(before.encode()),
    }
    return fake.tool_use(call_id, "edit", arguments)


def seed() -> list[fake.Step]:
    """The Coder's first implementation, against the inventory fixture as committed."""
    store = (FIXTURE / STORE).read_text(encoding="utf-8")
    init = (FIXTURE / INIT).read_text(encoding="utf-8")
    last = store.splitlines(keepends=True)[-1]
    new_init = init.replace(
        "from inventory.report import",
        "from inventory.receiving import receive\nfrom inventory.report import",
    ).replace('"low_stock", "to_csv"', '"low_stock", "receive", "to_csv"')
    read = {"start_line": None, "end_line": None}
    return [
        _turn(fake.tool_use("seed-1", "read_file", {"path": STORE, **read})),
        _turn(
            _edit("seed-2", STORE, last, last + RESTOCK, store),
            fake.tool_use(
                "seed-3", "create_file", {"path": RECEIVING, "content": RECEIVING_SEEDED}
            ),
            _edit("seed-4", INIT, init, new_init, init),
            fake.tool_use("seed-5", "create_file", {"path": UNIT_TEST, "content": UNIT_TESTS}),
        ),
        _turn(fake.tool_use("seed-6", "run", {"command": "pytest", "paths": [], "keyword": None})),
        fake.answer(SUMMARY),
    ]


_ROLE = re.compile(r"# Role: (\w+)")


def role_of(request: Request) -> str:
    found = _ROLE.match(request["system"][0]["text"])
    return found[1].lower() if found else ""


class Seeded:
    """A ``MessagesClient``: the Coder's first invocation from ``steps``, the rest real."""

    def __init__(self, inner: MessagesClient, steps: list[fake.Step]) -> None:
        self.inner = inner
        self.seed = fake.ScriptedClient(*steps)

    def _seeding(self, request: Request) -> bool:
        return role_of(request) == "coder" and not self.seed.exhausted

    async def create(self, request: Request) -> Response:
        if self._seeding(request):
            return await self.seed.create(request)
        return await self.inner.create(request)

    async def count_tokens(self, request: Request) -> int:
        if self._seeding(request):
            return estimate_input_tokens(request)  # placeholder signatures: never sent
        count: int = await self.inner.count_tokens(request)
        return count

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--"]:
        args = args[1:]
    real = cli._client

    def seeded(settings: MsaConfig, environment: LiveEnvironment) -> MessagesClient:
        return Seeded(real(settings, environment), seed())

    cli._client = seeded
    sys.argv = ["msa", *args]
    cli.main()


if __name__ == "__main__":
    main()
