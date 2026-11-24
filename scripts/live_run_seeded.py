"""Run ``msa`` with one thing planted, the Coder's first implementation or the Reviewer's
first review; every other model call is real.

Used by the `live run` workflow's ``seeded`` mode (.github/workflows/live-run.yml):

    uv run python scripts/live_run_seeded.py --plant code -- --config msa.toml run \\
        --repo ... --task-file .github/live-run/restock.md ...

Four live runs never raised a review finding: the real Coder's code kept passing review, so
neither the Reviewer catching a defect nor the negotiation that follows had run with the
real model. ``--plant code`` plants one known defect where the scripted definition-of-done
demo plants it, and lets the real roles find it. In live-7 and live-8 the real Tester's
tests caught it before review, so negotiation still never ran: ``--plant finding`` starts
it directly.

- **--plant code:** the Coder's first invocation only (``implement_plan``). It is
  answered from the script below instead of the model, and writes a correct
  ``Store.restock``, the export and passing unit tests, plus ``receive()`` with the planted
  bug: it applies a delivery note line by line, so a bad line halfway through leaves the
  store partly restocked. It passes lint, types, the security rules and every behaviour the
  task names.
- **restock-scale:** the task also states that receive must never scan or sort the whole
  store per line. The seed's receive is right in every other way, but checks each SKU
  through ``items()``, which sorts the whole store, once per line. No timing on a small
  store shows it; only a test or a reviewer that holds the code to the stated rule can.
  In live-8 the real Tester's tests did.
- **What is real:** every request of every other role (Planner, Tester, Reviewer, Arbiter,
  triage), and every later Coder request (revisions, answers to findings), go to the model
  through the client msa.toml configures. Whether anything catches the bug, and how, is
  the result.
- **--plant finding** (restock-atomic): the Reviewer's first review only. It is answered
  with one blocking correctness finding, written without reading the code: that receive()
  is not all or nothing. It points at the lines of src/inventory/receiving.py the request's
  changed_lines lists, so it is admitted wherever the real Coder put them. The task states
  that rule, and the real Tester's acceptance tests passed before review, so the claim is
  probably false. Everything else is real: the Planner, the Coder's implementation and its
  answer to the finding, the Reviewer's reply, the Tester's repro test and the Arbiter.
  Whether the finding is withdrawn, dismissed or upheld, and how, is the result.

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
# For restock-scale, whose task states that receive must never scan or sort the whole store
# per line: everything right (all or nothing, every check), except that each SKU is looked
# up through items(), which sorts the whole store, once per line. Small tests cannot see it.
RECEIVING_SCALE = '''"""Deliveries: "sku,quantity" lines, applied to a store all or nothing."""

from inventory.store import Store


def receive(store: Store, note: str) -> int:
    """Restock ``store`` from ``note``, all or nothing; returns the number of lines applied."""
    deliveries: list[tuple[str, int]] = []
    for line in note.splitlines():
        if not line.strip():
            continue
        name, _, quantity = line.partition(",")
        sku, amount = name.strip(), int(quantity)
        if amount <= 0:
            raise ValueError(f"{sku}: a restock must add a positive quantity")
        if sku not in [item.sku for item in store.items()]:
            raise KeyError(sku)
        deliveries.append((sku, amount))
    for sku, amount in deliveries:
        store.restock(sku, amount)
    return len(deliveries)
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


def seed(task: str = "restock") -> list[fake.Step]:
    """The Coder's first implementation, against the inventory fixture as committed: the
    scale defect for the restock-scale task, the line-by-line one for the others."""
    receiving = RECEIVING_SCALE if task == "restock-scale" else RECEIVING_SEEDED
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
            fake.tool_use("seed-3", "create_file", {"path": RECEIVING, "content": receiving}),
            _edit("seed-4", INIT, init, new_init, init),
            fake.tool_use("seed-5", "create_file", {"path": UNIT_TEST, "content": UNIT_TESTS}),
        ),
        _turn(fake.tool_use("seed-6", "run", {"command": "pytest", "paths": [], "keyword": None})),
        fake.answer(SUMMARY),
    ]


# ---------- --plant finding

CLAIM = (
    "receive() is not all or nothing: a note whose second line names an unknown SKU raises "
    "KeyError after the first line was already restocked, so the store is left changed."
)
EVIDENCE = (
    "receive() restocks each line as it reads it, so the lines before a bad one are already "
    'applied when it raises. With apple added at 1, receive(store, "apple,2\\nnope,1") '
    "raises KeyError and leaves apple at 3, against the task's rule that a bad note leaves "
    "the store exactly as it was."
)
_CHANGED = re.compile(r"<changed_lines>\n(.*?)\n</changed_lines>", re.DOTALL)


def changed(request: Request) -> dict[str, list[tuple[int, int]]]:
    """The changed_lines section of a review request, as line ranges per path."""
    content: Any = request["messages"][0]["content"]
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
    found = _CHANGED.search(text)
    ranges: dict[str, list[tuple[int, int]]] = {}
    for line in found[1].splitlines() if found else []:
        path, sep, spans = line.rpartition(": ")
        if not sep:
            continue
        for span in spans.split(", "):
            start, _, end = span.partition("-")
            if start.isdigit() and (end or start).isdigit():
                ranges.setdefault(path, []).append((int(start), int(end or start)))
    return ranges


def planted_review(request: Request) -> Response:
    """The Reviewer's first answer: one blocking finding on receive(), unread."""
    spans = changed(request).get(RECEIVING)
    if not spans:
        summary = f"(planted) {RECEIVING} was not changed, so no finding was planted."
        return fake.answer({"summary": summary, "findings": []})
    start, end = spans[0]
    finding = {
        "severity": "blocker",
        "kind": "correctness",
        "location": {"path": RECEIVING, "start_line": start, "end_line": end},
        "claim": CLAIM,
        "evidence": EVIDENCE,
        "suggested_fix": "Parse and check every line first, then restock them all.",
        "preference_ref": "",
        "reopens": "",
        "confidence": 0.9,
    }
    summary = "receive() breaks the task's all-or-nothing rule; the rest is sound."
    return fake.answer({"summary": summary, "findings": [finding]})


# ---------- the client

_ROLE = re.compile(r"# Role: (\w+)")


def role_of(request: Request) -> str:
    found = _ROLE.match(request["system"][0]["text"])
    return found[1].lower() if found else ""


class Seeded:
    """A ``MessagesClient``: ``role``'s first invocation from ``steps``, the rest real."""

    def __init__(self, inner: MessagesClient, steps: list[fake.Step], role: str = "coder") -> None:
        self.inner = inner
        self.role = role
        self.seed = fake.ScriptedClient(*steps)

    def _seeding(self, request: Request) -> bool:
        return role_of(request) == self.role and not self.seed.exhausted

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


def task_of(args: list[str]) -> str:
    """The task's name, from ``--task-file .github/live-run/<name>.md``."""
    if "--task-file" in args and args.index("--task-file") + 1 < len(args):
        return Path(args[args.index("--task-file") + 1]).stem
    return "restock"


PLANTS = ("code", "finding")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    plant = "code"
    if args[:1] == ["--plant"]:
        plant, args = (args[1] if len(args) > 1 else ""), args[2:]
    if plant not in PLANTS:
        raise SystemExit(f"--plant must be one of {', '.join(PLANTS)}, not {plant!r}")
    if args[:1] == ["--"]:
        args = args[1:]
    real = cli._client
    task = task_of(args)

    def seeded(settings: MsaConfig, environment: LiveEnvironment) -> MessagesClient:
        if plant == "finding":
            return Seeded(real(settings, environment), [planted_review], role="reviewer")
        return Seeded(real(settings, environment), seed(task))

    cli._client = seeded
    sys.argv = ["msa", *args]
    cli.main()


if __name__ == "__main__":
    main()
