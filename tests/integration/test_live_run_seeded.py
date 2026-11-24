"""scripts/live_run_seeded.py: the seeded live run's seed, and what it hands to the model.

The seed must be a real implementation of the restock task that the gates accept, so that
only its planted bug is left for review to find. Its only scripted invocation must be the
Coder's first: every other request goes to the client it wraps.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from msa.core.values import Phase
from msa.llm import fake
from msa.llm.client import Request, Response
from tests.integration.test_demo import PLANNER, TASK, TESTER
from tests.support.runs import Harness, RoleScripts, make_harness
from tests.support.scripts import load

pytestmark = pytest.mark.slow

seeded = load("live_run_seeded")


def records(harness: Harness, name: str) -> list[dict[str, Any]]:
    path = harness.run_dir / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    ("task", "planted"),
    [
        ("restock", "+        store.restock(sku.strip(), int(quantity))"),
        ("restock-scale", "+        if sku not in [item.sku for item in store.items()]:"),
    ],
)
def test_the_seed_passes_every_gate_with_only_its_planted_bug_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str, planted: str
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    harness = make_harness(tmp_path, fixture="inventory", task=TASK)  # review: stand-in
    inner = RoleScripts(planner=PLANNER, tester=TESTER[:2])  # no Coder script at all
    client = seeded.Seeded(inner, seeded.seed(task))

    final, journal = harness.run(client, approve_release=False)
    journal.close()
    assert final.phase is Phase.HUMAN_APPROVAL, final.outcome  # every gate passed
    assert final.revisions == 0
    assert client.seed.exhausted  # the Coder's one invocation was the seed, whole
    patch = (harness.run_dir / "patch.diff").read_text(encoding="utf-8")
    assert planted in patch  # the planted bug, and nothing caught it
    assert '+            raise ValueError("a restock must add a positive quantity")' in patch


def _id_in(request: Request) -> str:
    content = request["messages"][0]["content"]
    found = re.search(r"\bc\d+-1\b", content if isinstance(content, str) else str(content))
    assert found, "the request names the planted finding"
    return found[0]


def _rebut(request: Request) -> Response:
    move = {
        "finding_id": _id_in(request),
        "action": "rebut",
        "rationale": "receive() checks every line before it restocks any",
        "evidence": "the first loop only parses and checks; restock runs in a second loop",
    }
    return fake.answer({"moves": [move]})


def _withdraw(request: Request) -> Response:
    move = {
        "finding_id": _id_in(request),
        "action": "withdraw",
        "rationale": "the two loops make it all or nothing",
        "amendment": "",
    }
    return fake.answer({"moves": [move]})


def test_the_planted_finding_is_admitted_and_negotiated_by_the_roles_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MSA_USER_PREFERENCES", str(tmp_path / "no-user-layer.yaml"))
    harness = make_harness(tmp_path, review=True, fixture="inventory", task=TASK)
    inner = RoleScripts(
        planner=PLANNER,
        tester=TESTER[:2],
        coder=[*seeded.seed("restock-scale"), _rebut],  # an all-or-nothing receive
        reviewer=[_withdraw],  # only the reply: the first review is the plant
    )
    client = seeded.Seeded(inner, [seeded.planted_review], role="reviewer")

    final, journal = harness.run(client, approve_release=False)
    journal.close()
    assert final.phase is Phase.HUMAN_APPROVAL, final.outcome
    assert client.seed.exhausted  # the first review was the plant
    assert inner.exhausted  # and every scripted stand-in answered
    (finding,) = records(harness, "findings.jsonl")
    assert finding["admitted"]
    assert finding["severity"] == "blocker"
    assert finding["finding"]["location"]["path"] == seeded.RECEIVING
    assert finding["finding"]["claim"] == seeded.CLAIM
    (outcome,) = records(harness, "negotiation.jsonl")
    assert outcome["verdict"] == "withdrawn"
    (reply,) = inner.requests("reviewer")  # the real Reviewer sees the claim and the rebuttal
    assert seeded.CLAIM in str(reply["messages"][0]["content"])


def test_a_planted_review_with_nothing_to_point_at_plants_nothing() -> None:
    request = {
        "model": "m",
        "system": [{"type": "text", "text": "# Role: Reviewer\n"}],
        "messages": [
            {"role": "user", "content": "<changed_lines>\nsrc/x.py: 1-3\n</changed_lines>"}
        ],
    }
    assert seeded.changed(request) == {"src/x.py": [(1, 3)]}
    answer = json.loads(seeded.planted_review(request)["content"][-1]["text"])
    assert answer["findings"] == []
    assert "no finding was planted" in answer["summary"]


def test_only_the_coders_first_invocation_is_seeded() -> None:
    def request(role: str, n: int) -> dict[str, object]:
        return {
            "model": "m",
            "system": [{"type": "text", "text": f"# Role: {role}\n"}],
            "messages": [{"role": "user", "content": f"turn {n}"}],
        }

    inner = fake.ScriptedClient(fake.answer({"from": "the model"}), fake.answer({"again": 1}))
    client = seeded.Seeded(inner, [fake.answer({"from": "the seed"})])

    async def go() -> list[object]:
        return [
            await client.create(request("tester", 1)),  # another role: the model
            await client.create(request("coder", 2)),  # the Coder's first: the seed
            await client.create(request("coder", 3)),  # the Coder again: the model
        ]

    tester, coder_first, coder_later = asyncio.run(go())
    assert "the model" in str(tester)
    assert "the seed" in str(coder_first)
    assert '"again"' in str(coder_later)
    assert [r["messages"][0]["content"] for r in inner.requests] == ["turn 1", "turn 3"]


def test_the_seeded_command_runs_msa_with_the_seeding_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(seeded.cli, "main", lambda: seen.append(list(sys.argv)))
    real = seeded.cli._client
    try:
        seeded.main(["--", "--config", "x.toml", "run", "--repo", "r"])
        assert seen == [["msa", "--config", "x.toml", "run", "--repo", "r"]]
        assert seeded.cli._client is not real  # wrapped
    finally:
        seeded.cli._client = real


def test_the_seed_follows_the_task_file() -> None:
    assert seeded.task_of(["run", "--task-file", ".github/live-run/restock-scale.md"]) == (
        "restock-scale"
    )
    assert seeded.task_of(["run", "--task", "x"]) == "restock"


@pytest.mark.parametrize(("plant", "role"), [([], "coder"), (["--plant", "finding"], "reviewer")])
def test_the_plant_picks_the_role_seeded(
    monkeypatch: pytest.MonkeyPatch, plant: list[str], role: str
) -> None:
    monkeypatch.setattr(seeded.cli, "main", lambda: None)
    monkeypatch.setattr(seeded.cli, "_client", lambda settings, environment: "the real client")
    seeded.main([*plant, "--", "run", "--task-file", ".github/live-run/restock-atomic.md"])
    client = seeded.cli._client(None, None)
    assert client.role == role
    assert client.inner == "the real client"


def test_an_unknown_plant_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeded.cli, "main", lambda: pytest.fail("msa ran"))
    with pytest.raises(SystemExit, match="--plant must be one of code, finding"):
        seeded.main(["--plant", "bug", "--", "run"])
