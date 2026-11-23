"""The definition-of-done run (ADR-015): one multi-file task, every stage, a crash, a PR.

The task changes four files of the inventory fixture. The model is scripted per role (no
live model is used in tests), and the scripted Coder makes four deliberate mistakes, each
of a kind a different stage exists to catch. The run passes only if each one is caught by
that stage, fixed by a revision, and every gate passes again on the revised tree:

1. ``print()`` in library code. The repository's own preference layer makes
   ``py.output.no-print`` hard, so STATIC_ANALYSIS fails it (ruff T201). Without that
   layer it would only be a soft warning: the preference is honored as written.
2. ``restock()`` accepts a quantity that is not positive. The Tester wrote the acceptance
   tests blind, from the plan; one fails in UNIT_TESTING, the Tester diagnoses
   ``impl_bug``, and the Coder revises.
3. ``eval()`` to parse a quantity. SECURITY_REVIEW finds the call in the syntax tree of
   the added lines. It is high severity, so it is not negotiable: it goes straight back.
4. ``receive()`` applies a delivery line by line, so a bad line leaves the store half
   updated. No test catches that; the Reviewer does. The Coder accepts the finding in
   NEGOTIATION, and the revision makes it all or nothing.

Everything else is real: git and the worktrees, the sandbox, ruff, pytest (the integration
suite in a network namespace of its own where the kernel allows it), the security rules,
the journal and the response store on disk, and the published branch.

The crash is a real SIGKILL. The run starts in a forked child, which kills itself in the
middle of the Coder's third revision: after it edited a file, before its final answer.
The parent then resumes the run from the journal, as ``msa resume`` would. No effect whose
completion was journaled runs again; the revision in flight runs again with its paid turns
replayed from the response store, so its edit is not applied twice; and the run ends in a
branch a person approved, with every gate's evidence in the PR description.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from msa.core.effects import Effect, ReviseChanges
from msa.core.events import Completion
from msa.core.values import Phase
from msa.llm import fake
from msa.llm.client import Request, Response
from msa.llm.store import SqliteResponseStore
from msa.observability.report import timeline
from msa.runtime.interpreter import Observer
from msa.runtime.journal import SqliteJournal
from msa.tools.sandbox import network_namespace
from tests.support import netns
from tests.support.runs import (
    APPROVER,
    RELEASE,
    RUN,
    Harness,
    RoleScripts,
    create,
    edit,
    make_harness,
    phases,
    read,
    tool_results,
    tool_turn,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork() to crash a child"),
]

TASK = (
    "Add Store.restock(sku, quantity) to src/inventory/store.py: it adds quantity to an "
    "existing item's stock, raises ValueError for a quantity that is not positive, and "
    "raises KeyError for an unknown SKU. Add receive(store, note) in a new module "
    'src/inventory/receiving.py that applies a delivery note of "sku,quantity" lines with '
    "restock and returns how many lines it applied, and export it from the package."
)

# ---------- the code, as the scripted Coder writes and revises it

STORE = "src/inventory/store.py"
RECEIVING = "src/inventory/receiving.py"
INIT = "src/inventory/__init__.py"
UNIT_TEST = "tests/test_restock.py"
ACCEPTANCE = "tests/acceptance/test_restocking.py"  # a basename no other test module has

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "inventory"
BASE_STORE = (FIXTURE / STORE).read_text(encoding="utf-8")
BASE_INIT = (FIXTURE / INIT).read_text(encoding="utf-8")
LAST_STORE_LINE = "        return sorted(self._items.values(), key=lambda item: item.sku)\n"

PRINTED = '        print(f"restocking {sku}")\n'
UNCHECKED = """
    def restock(self, sku: str, quantity: int) -> None:
        item = self._items[sku]
        self._items[sku] = Item(sku, item.quantity + quantity)
"""
CHECK = (
    "        if quantity <= 0:\n"
    '            raise ValueError("a restock must add a positive quantity")\n'
)
SIGNATURE = "    def restock(self, sku: str, quantity: int) -> None:\n"
STORE_V1 = BASE_STORE + UNCHECKED.replace(SIGNATURE, SIGNATURE + PRINTED)  # bug 1 and bug 2
STORE_V2 = BASE_STORE + UNCHECKED  # bug 2
STORE_V3 = BASE_STORE + UNCHECKED.replace(SIGNATURE, SIGNATURE + CHECK)

RECEIVING_V1 = '''"""Deliveries: a note of "sku,quantity" lines, applied to a store."""

from inventory.store import Store


def receive(store: Store, note: str) -> int:
    """Restock ``store`` from ``note``; returns the number of lines applied."""
    applied = 0
    for line in note.splitlines():
        if not line.strip():
            continue
        sku, quantity = line.split(",")
        store.restock(sku.strip(), eval(quantity))
        applied += 1
    return applied
'''  # bug 3 (line 13) and bug 4 (lines 9-14)
RECEIVING_V2 = RECEIVING_V1.replace("eval(quantity)", "int(quantity)")  # bug 4
RECEIVING_V3 = '''"""Deliveries: a note of "sku,quantity" lines, applied to a store."""

from inventory.store import Store


def receive(store: Store, note: str) -> int:
    """Restock ``store`` from ``note``, all or nothing; returns the number of lines applied.

    Every line is parsed and checked before any is applied, so a bad line leaves the store
    as it was."""
    deliveries = [_parse(line) for line in note.splitlines() if line.strip()]
    for sku, quantity in deliveries:
        if quantity <= 0:
            raise ValueError(f"{sku}: a restock must add a positive quantity")
        store.quantity(sku)  # raises KeyError for an unknown SKU
    for sku, quantity in deliveries:
        store.restock(sku, quantity)
    return len(deliveries)


def _parse(line: str) -> tuple[str, int]:
    sku, quantity = line.split(",")
    return sku.strip(), int(quantity)
'''
NEW_INIT = BASE_INIT.replace(
    "from inventory.report import",
    "from inventory.receiving import receive\nfrom inventory.report import",
).replace('"low_stock", "to_csv"', '"low_stock", "receive", "to_csv"')

UNIT_TEST_V1 = """from inventory import Store, receive


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
UNIT_TEST_V2 = (
    "import pytest\n\n"
    + UNIT_TEST_V1
    + """

def test_a_bad_line_leaves_the_store_unchanged() -> None:
    store = Store()
    store.add("apple", 1)
    with pytest.raises(ValueError, match="positive"):
        receive(store, "apple,2\\napple,0\\n")
    assert store.quantity("apple") == 1
"""
)

ACCEPTANCE_TESTS = """import pytest

from inventory import Store, receive


def test_restock_adds_to_an_existing_item() -> None:
    store = Store()
    store.add("bolt", 10)
    store.restock("bolt", 5)
    assert store.quantity("bolt") == 15


@pytest.mark.parametrize("quantity", [0, -3])
def test_restock_rejects_a_quantity_that_is_not_positive(quantity: int) -> None:
    store = Store()
    store.add("bolt", 10)
    with pytest.raises(ValueError):
        store.restock("bolt", quantity)
    assert store.quantity("bolt") == 10


def test_restock_of_an_unknown_sku_raises_key_error() -> None:
    with pytest.raises(KeyError):
        Store().restock("nut", 1)


def test_receive_applies_a_delivery_note() -> None:
    store = Store()
    store.add("bolt", 1)
    store.add("nut", 2)
    assert receive(store, "bolt,4\\nnut,1\\n") == 2
    assert (store.quantity("bolt"), store.quantity("nut")) == (5, 3)
"""

# ---------- the roles' scripts

CRITERIA = [
    "restock adds the quantity to an existing item",
    "restock raises ValueError for a quantity that is not positive",
    "restock raises KeyError for an unknown SKU",
    "receive applies each line of a delivery note and returns the count",
]
PLAN = {
    "summary": "Add Store.restock, a receiving module that applies delivery notes, and tests.",
    "steps": [
        {"description": "Add Store.restock with its two checks.", "files": [STORE]},
        {"description": "Add receive() in a new receiving module.", "files": [RECEIVING]},
        {"description": "Export receive from the package.", "files": [INIT]},
        {"description": "Unit tests for restock and receive.", "files": [UNIT_TEST]},
    ],
    "acceptance_criteria": CRITERIA,
    "risks": ["A delivery note with a malformed line."],
}
FILES = [STORE, RECEIVING, INIT, UNIT_TEST]
NON_POSITIVE = f"{ACCEPTANCE}::test_restock_rejects_a_quantity_that_is_not_positive"


def summary(text: str) -> fake.Step:
    return fake.answer(
        {"summary": text, "files_changed": FILES, "checks_run": [], "open_issues": []}
    )


PLANNER = [tool_turn(read("p1", STORE)), fake.answer(PLAN)]
TESTER = [
    tool_turn(create("t1", ACCEPTANCE, ACCEPTANCE_TESTS)),
    fake.answer(
        {
            "summary": "restock's three rules and receive, from the plan alone.",
            "tests": [
                f"{ACCEPTANCE}::test_restock_adds_to_an_existing_item",
                NON_POSITIVE,
                f"{ACCEPTANCE}::test_restock_of_an_unknown_sku_raises_key_error",
                f"{ACCEPTANCE}::test_receive_applies_a_delivery_note",
            ],
            "criteria_covered": CRITERIA,
            "open_issues": [],
        }
    ),
    # Triage of the failing acceptance test, once UNIT_TESTING asks.
    fake.answer(
        {
            "kind": "impl_bug",
            "test_id": f"{NON_POSITIVE}[0]",
            "diagnosis": "Store.restock never checks quantity, so restock(sku, 0) succeeds; "
            "the plan says it raises ValueError.",
        }
    ),
]
IMPLEMENT = [
    tool_turn(read("c1", STORE)),
    tool_turn(
        edit(
            "c2", STORE, LAST_STORE_LINE, LAST_STORE_LINE + STORE_V1[len(BASE_STORE) :], BASE_STORE
        ),
        create("c3", RECEIVING, RECEIVING_V1),
        edit("c4", INIT, BASE_INIT, NEW_INIT, BASE_INIT),
        create("c5", UNIT_TEST, UNIT_TEST_V1),
    ),
    tool_turn(fake.tool_use("c6", "run", {"command": "pytest", "paths": [], "keyword": None})),
    summary("Added restock, receive and their tests."),
]
REVISION_1 = [  # STATIC_ANALYSIS: the hard preference
    tool_turn(edit("r1", STORE, PRINTED, "", STORE_V1)),
    summary("Removed the print() call: py.output.no-print is hard in this repository."),
]
REVISION_2 = [  # UNIT_TESTING: the triaged acceptance failure
    tool_turn(edit("r2", STORE, SIGNATURE, SIGNATURE + CHECK, STORE_V2)),
    summary("restock now raises ValueError for a quantity that is not positive."),
]
REVISION_3 = [  # SECURITY_REVIEW: eval
    tool_turn(read("s1", RECEIVING)),
    tool_turn(edit("s2", RECEIVING, "eval(quantity)", "int(quantity)", RECEIVING_V1)),
    summary("Parse the quantity with int(), never eval()."),
]
REVISION_4 = [  # NEGOTIATION: the accepted finding
    tool_turn(
        edit("f1", RECEIVING, RECEIVING_V2, RECEIVING_V3, RECEIVING_V2),
        edit("f2", UNIT_TEST, UNIT_TEST_V1, UNIT_TEST_V2, UNIT_TEST_V1),
    ),
    summary("receive checks every line before it applies any, with a test for it."),
]
FINDING_ID = "c3-1"  # the first finding of the review after revision 3
FINDING = {
    "severity": "major",
    "kind": "correctness",
    "location": {"path": RECEIVING, "start_line": 9, "end_line": 14},
    "claim": "receive() is not atomic: a bad line raises after the lines before it were "
    "applied, so a rejected delivery leaves the store half updated.",
    "evidence": f"{RECEIVING} lines 9-14 call restock inside the loop that parses the note",
    "suggested_fix": "Parse and check every line first, then restock.",
    "preference_ref": "",
    "reopens": "",
    "confidence": 0.9,
}
REVIEWER = [
    fake.answer({"summary": "One correctness problem.", "findings": [FINDING]}),
    fake.answer({"summary": "The revision makes receive all or nothing.", "findings": []}),
]
ACCEPT = fake.answer(
    {
        "moves": [
            {
                "finding_id": FINDING_ID,
                "action": "accept",
                "rationale": "accept: a rejected delivery must not change the store",
                "evidence": "",
            }
        ]
    }
)
# The crash: SIGKILL when the Coder asks for the third revision's last turn, after its edit.
BEFORE_CRASH = [*IMPLEMENT, *REVISION_1, *REVISION_2, *REVISION_3[:-1]]
AFTER_CRASH = [REVISION_3[-1], ACCEPT, *REVISION_4]


# ---------- the crash


class KilledAt(RoleScripts):
    """The scripted model, which SIGKILLs this process when ``role`` asks for its
    ``nth`` response: no cleanup, no flush, no rollback."""

    def __init__(self, role: str, nth: int, **scripts: Iterable[fake.Step]) -> None:
        super().__init__(**scripts)
        self.kill = (role, nth)

    async def create(self, request: Request) -> Response:
        role = self.role_of(request)
        if (role, len(self.requests(role)) + 1) == self.kill:
            os.kill(os.getpid(), signal.SIGKILL)
        return await super().create(request)


class Started(Observer):
    """Logs every effect an executor starts. O_APPEND writes survive SIGKILL."""

    def __init__(self, log: Path) -> None:
        self.log = log

    def effect_started(self, effect: Effect) -> None:
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, f"{effect.id} {effect.command.kind}\n".encode())
        finally:
            os.close(fd)

    def lines(self) -> list[tuple[str, str]]:
        text = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return [(line.split()[0], line.split()[1]) for line in text.splitlines()]


def _crashing_run(harness: Harness) -> None:
    client = KilledAt(
        "coder",
        len(BEFORE_CRASH) + 1,
        planner=PLANNER,
        tester=TESTER,
        coder=[*BEFORE_CRASH, *AFTER_CRASH],
    )
    harness.run(client, approve_release=False)


# ---------- the run


class OnDisk:
    """The run's response store on disk, as msa run keeps it: one connection per process,
    since a SQLite connection must not cross a fork."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.opened: dict[int, SqliteResponseStore] = {}

    def __call__(self) -> SqliteResponseStore:
        if os.getpid() not in self.opened:
            self.opened[os.getpid()] = SqliteResponseStore(self.path)
        return self.opened[os.getpid()]

    def close(self) -> None:
        for store in self.opened.values():
            store.close()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    harness = make_harness(tmp_path, review=True, fixture="inventory", task=TASK)
    harness.store = store = OnDisk(harness.state_dir / "responses.db")
    harness.observer = Started(tmp_path / "started.log")
    yield harness
    store.close()


def records(harness: Harness, name: str) -> list[dict[str, Any]]:
    text = (harness.run_dir / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout


def test_the_definition_of_done_run(harness: Harness) -> None:
    started = harness.observer
    assert isinstance(started, Started)
    checkout = git(harness.repo, "rev-parse", "HEAD"), git(harness.repo, "status", "--porcelain")

    # The run, in a child that SIGKILLs itself mid-revision.
    child = multiprocessing.get_context("fork").Process(target=_crashing_run, args=(harness,))
    child.start()
    child.join(timeout=600)
    assert child.exitcode == -signal.SIGKILL, f"the child exited with {child.exitcode}"

    with SqliteJournal(harness.state_dir / "runs.db") as journal:
        crashed = journal.load(RUN)
        completed = {e.effect_id for e in journal.events(RUN) if isinstance(e, Completion)}
    assert crashed.phase is Phase.REWORK
    (in_flight,) = crashed.pending
    assert isinstance(in_flight.command, ReviseChanges)
    assert in_flight.id not in completed
    before_resume = len(started.lines())

    # The parent resumes it from disk; a person approves the release; it publishes.
    client = RoleScripts(planner=[], tester=[], coder=AFTER_CRASH, reviewer=REVIEWER)
    final, journal = harness.resume(client)
    with journal:
        assert final.phase is Phase.COMPLETED, final.outcome
        assert journal.verify(RUN).ok
        timeline_lines = timeline(journal.events(RUN))
    seen = phases(timeline_lines)
    assert client.exhausted
    assert final.revisions == 4

    # No journaled effect ran again; the one in flight ran again first.
    rerun = started.lines()[before_resume:]
    assert rerun[0] == (in_flight.id, "revise_changes")
    assert not {effect for effect, _ in rerun} & completed
    # Its paid turns were replayed, not re-run: the first request after the crash is the
    # answer turn, and it carries the recorded result of the edit made before the crash.
    # Had the edit run again, its expected hash would no longer match the file.
    (edited,) = tool_results(client.requests("coder")[0])
    assert not edited.get("is_error")
    assert str(edited["content"]).startswith(f"edited {RECEIVING} sha256:")

    # Each seeded bug was caught by the stage meant for it, in order.
    reworked = [line.split(" -> ")[0] for line in seen if line.endswith("-> REWORK")]
    assert reworked == ["STATIC_ANALYSIS", "UNIT_TESTING", "SECURITY_REVIEW", "NEGOTIATION"]
    assert seen[-3:] == RELEASE

    static = records(harness, "verifications.jsonl")
    assert [v["failed"] for v in static] == [["preferences"], [], [], [], []]
    first_static = harness.artifact(static[0]["report"])
    assert "py.output.no-print" in first_static
    assert "1 hard" in first_static

    suites = records(harness, "tests.jsonl")
    assert (suites[0]["verdict"], suites[0]["kind"]) == ("failed", "impl_bug")
    assert all(s["verdict"] == "passed" for s in suites[1:])
    assert "## Triage: impl_bug (owner: coder)" in harness.artifact(suites[0]["report"])

    security = records(harness, "security.jsonl")
    assert [s["verdict"] for s in security] == ["failed", "passed", "passed"]
    assert f"{RECEIVING}:13" in harness.artifact(security[0]["report"])
    assert "eval" in harness.artifact(security[0]["report"])

    (negotiated,) = records(harness, "negotiation.jsonl")
    assert (negotiated["finding"], negotiated["author"], negotiated["verdict"]) == (
        FINDING_ID,
        "accept",
        "accepted",
    )

    integration = records(harness, "integrations.jsonl")
    assert {(i["verdict"], i["ran"]) for i in integration} == {("passed", True)}
    isolated = netns.isolation(network_namespace() is not None)
    assert {i["isolated"] for i in integration} == {isolated}

    # The published branch: the approved tree, the person named, the checkout untouched.
    branch = "aegis/run-1"
    message = git(harness.repo, "log", "-1", "--format=%B", branch)
    assert f"Approved-by: {APPROVER}" in message
    assert git(harness.repo, "rev-parse", f"{branch}^{{tree}}").strip() == final.head_tree
    shipped = {p: git(harness.repo, "show", f"{branch}:{p}") for p in [STORE, RECEIVING]}
    assert shipped[STORE] == STORE_V3
    assert shipped[RECEIVING] == RECEIVING_V3
    assert all("print(" not in s and "eval(" not in s for s in shipped.values())
    assert (git(harness.repo, "rev-parse", "HEAD"), git(harness.repo, "status", "--porcelain")) == (
        checkout
    )
    patch = harness.run_dir / "patch.diff"
    git(harness.repo, "apply", "--check", str(patch))

    # The PR description: every gate's evidence on the delivered tree, what each gate caught
    # on the way there, and how the review was settled, for the person who approved it.
    pr = (harness.run_dir / "pr.md").read_text(encoding="utf-8")
    assert pr.startswith("# Add Store.restock(sku, quantity) to src/inventory/store.py\n")
    for gate in ("Static analysis", "Unit tests", "Security review"):
        assert f"| {gate} | passed | `sha256:" in pr, gate
    network = "without network" if isolated else "with network"
    assert f"| Integration tests | passed, {network} |" in pr
    caught = pr.split("## Caught along the way\n\n", 1)[1].split("\n\n## ", 1)[0]
    assert [row.split(" | ")[0::2] for row in caught.splitlines()[4:]] == [
        ["| Static analysis", "`preferences`"],
        ["| Unit tests", "impl_bug"],
        ["| Security review", "1 blocking finding(s)"],
    ]
    assert f"- `{FINDING_ID}` [major] receive() is not atomic" in pr
    assert "half updated: accepted in negotiation" in pr

    if os.environ.get("MSA_UPDATE_DEMO") == "1":
        _export(harness, pr, timeline_lines)


DEMO = Path(__file__).resolve().parents[2] / "docs" / "demo"


def _export(harness: Harness, pr: str, lines: list[str]) -> None:
    """A snapshot of one run for docs/demo. Not compared: hashes, timings and digests
    change from run to run, so the assertions above are the test, not these files."""
    DEMO.mkdir(exist_ok=True)
    (DEMO / "pr.md").write_text(pr, encoding="utf-8")
    (DEMO / "timeline.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    patch = (harness.run_dir / "patch.diff").read_text(encoding="utf-8")
    (DEMO / "patch.diff").write_text(patch, encoding="utf-8")
