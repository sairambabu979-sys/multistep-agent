"""I-12 and I-13 (PLAN §6.2): a real worktree, the real tool layer, a scripted model.

The gateway, the toolset and the workspace are the production code; only the model's
replies are scripted. Each scenario checks what the model is told, what reaches the disk,
and what the operator sees.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from msa.agents.toolset import toolset
from msa.config import Role
from msa.llm import fake
from msa.llm.gateway import Gateway, InvocationResult
from tests.support.llm import VERDICT, Verdict, config, invocation
from tests.support.tools import Tree, build_tree


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path)


def invoke(
    tree: Tree, role: Role, *turns: dict[str, Any]
) -> tuple[InvocationResult[Verdict], fake.ScriptedClient]:
    workspace, sandbox = tree.workspace(role), tree.sandbox(role)
    client = fake.ScriptedClient(*turns, fake.answer(VERDICT))
    result = asyncio.run(
        Gateway(client, config()).invoke(invocation(role=role, tools=toolset(workspace, sandbox)))
    )
    return result, client


def calls(*uses: dict[str, Any]) -> dict[str, Any]:
    return fake.message(fake.thinking(), *uses, stop_reason="tool_use")


def results_of(client: fake.ScriptedClient, turn: int) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = client.requests[turn]["messages"][-1]["content"]
    return content


def test_i12_the_coder_cannot_edit_a_tester_owned_file_and_the_run_continues(tree: Tree) -> None:
    owned = tree.repo / "tests" / "acceptance" / "test_login.py"
    before = owned.read_text()
    view = tree.workspace(Role.CODER).read_file("tests/acceptance/test_login.py")
    result, client = invoke(
        tree,
        Role.CODER,
        calls(
            fake.tool_use(
                "e1",
                "edit",
                {
                    "path": "tests/acceptance/test_login.py",
                    "old": "pass",
                    "new": "assert True  # weakened",
                    "expected_hash": view.content_hash,
                },
            ),
            fake.tool_use(
                "e2",
                "create_file",
                {"path": "tests/acceptance/test_extra.py", "content": "def test_x(): pass\n"},
            ),
            fake.tool_use(
                "e3",
                "edit",
                {
                    "path": "owned_link",
                    "old": "pass",
                    "new": "x",
                    "expected_hash": view.content_hash,
                },
            ),
        ),
        calls(fake.tool_use("c1", "create_file", {"path": "src/feature.py", "content": "x = 1\n"})),
    )
    denied = results_of(client, 1)
    assert [r["is_error"] for r in denied] == [True, True, True]
    for r in denied:
        assert r["content"].startswith("CapabilityDenied: coder may not write 'tests/acceptance/")
        assert r["content"].endswith("it belongs to tester")
    assert results_of(client, 2)[0].get("is_error") is None  # the run carried on and wrote src
    assert result.output == Verdict(status="ok", notes="fine")
    assert owned.read_text() == before
    assert not (tree.repo / "tests" / "acceptance" / "test_extra.py").exists()
    assert (tree.repo / "src" / "feature.py").read_text() == "x = 1\n"
    assert [(v.kind, v.tool) for v in tree.violations] == [
        ("capability_denied", "edit"),
        ("capability_denied", "create_file"),
        ("capability_denied", "edit"),
    ]


ESCAPES: list[tuple[str, dict[str, Any]]] = [
    ("read_file", {"path": "../../etc/passwd", "start_line": None, "end_line": None}),
    ("read_file", {"path": "/etc/passwd", "start_line": None, "end_line": None}),
    ("read_file", {"path": "escape/secret.txt", "start_line": None, "end_line": None}),
    ("read_file", {"path": "secret_link", "start_line": None, "end_line": None}),
    ("create_file", {"path": "escape/planted.txt", "content": "x"}),
    ("create_file", {"path": "src/../../outside/planted.txt", "content": "x"}),
    ("edit", {"path": "secret_link", "old": "secret", "new": "x", "expected_hash": "sha256:0"}),
]


def test_i13_path_escapes_are_rejected_and_logged(tree: Tree) -> None:
    uses = [fake.tool_use(f"t{i}", name, args) for i, (name, args) in enumerate(ESCAPES)]
    uses.append(
        fake.tool_use("ok", "run", {"command": "pytest", "paths": ["../outside"], "keyword": None})
    )
    result, client = invoke(tree, Role.CODER, calls(*uses))
    replies = results_of(client, 1)
    assert all(r["is_error"] for r in replies)
    assert all(r["content"].startswith("PathRejected: ") for r in replies)
    assert "secret outside" not in str(client.requests)  # nothing of it reached the model
    assert result.output == Verdict(status="ok", notes="fine")
    assert tree.outside_untouched()
    assert [(v.kind, v.tool, v.path) for v in tree.violations] == [
        *(("path_rejected", name, args["path"]) for name, args in ESCAPES),
        ("path_rejected", "run", "../outside"),
    ]


def test_read_only_tools_are_marked_parallel_safe_and_writes_are_not(tree: Tree) -> None:
    tools = toolset(tree.workspace(), tree.sandbox())
    assert {t.name: t.parallel_safe for t in tools} == {
        "read_file": True,
        "search": True,
        "git_diff": True,
        "edit": False,
        "create_file": False,
        "run": False,
    }
