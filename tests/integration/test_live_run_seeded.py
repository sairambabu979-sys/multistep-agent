"""scripts/live_run_seeded.py: the seeded live run's seed, and what it hands to the model.

The seed must be a real implementation of the restock task that the gates accept, so that
only its planted bug is left for review to find. Its only scripted invocation must be the
Coder's first: every other request goes to the client it wraps.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from msa.core.values import Phase
from msa.llm import fake
from tests.integration.test_demo import PLANNER, TASK, TESTER
from tests.support.runs import RoleScripts, make_harness
from tests.support.scripts import load

pytestmark = pytest.mark.slow

seeded = load("live_run_seeded")


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
