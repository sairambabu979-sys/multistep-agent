"""The agent-facing tools: what each returns to the model."""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from msa.agents.toolset import toolset
from msa.config import Role
from msa.llm.gateway import Tool, ToolOutput
from msa.llm.schema import strictness_problems
from tests.support.tools import Tree, build_tree


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return build_tree(tmp_path)


def tools(tree: Tree) -> dict[str, Tool[Any]]:
    sandbox = tree.sandbox(launcher=(sys.executable, "-m"))
    return {t.name: t for t in toolset(tree.workspace(Role.CODER), sandbox)}


def call(tool: Tool[Any], **arguments: Any) -> ToolOutput:
    async def invoke() -> ToolOutput:
        return await tool.handler(tool.input_model.model_validate(arguments))

    return asyncio.run(invoke())


def test_every_tool_schema_is_strict_and_uses_only_supported_keywords(tree: Tree) -> None:
    for tool in tools(tree).values():
        definition = tool.definition()
        assert definition["strict"] is True
        assert strictness_problems(definition["input_schema"]) == [], tool.name


def test_read_then_edit_round_trips_through_the_hash(tree: Tree) -> None:
    by_name = tools(tree)
    shown = call(by_name["read_file"], path="src/app.py", start_line=None, end_line=None)
    assert shown.content.startswith("src/app.py (lines 1-2 of 2) sha256:")
    digest = re.findall(r"sha256:[0-9a-f]+", shown.content)[0]
    edited = call(by_name["edit"], path="src/app.py", old="hello", new="hi", expected_hash=digest)
    assert edited.content.startswith("edited src/app.py sha256:")
    created = call(by_name["create_file"], path="src/new.py", content="x = 1\n")
    assert created.content.startswith("created src/new.py sha256:")


def test_a_failing_command_is_an_error_result(tree: Tree) -> None:
    (tree.repo / "tests" / "test_fail.py").write_text("def test_x():\n    assert False\n")
    failed = call(tools(tree)["run"], command="pytest", paths=["tests/test_fail.py"], keyword=None)
    assert failed.is_error
    assert failed.content.startswith("$ pytest -q ./tests/test_fail.py\nexit 1 after")


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_search_and_git_diff_render_for_the_model(tree: Tree) -> None:
    by_name = tools(tree)
    assert call(by_name["search"], pattern="greet", glob=None).content == (
        "src/app.py:1: def greet(name):"
    )
    with pytest.raises(Exception, match="git diff failed"):  # not a git repository
        call(by_name["git_diff"], base=None)
