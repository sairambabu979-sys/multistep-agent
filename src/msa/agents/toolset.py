"""The tool surface an agent sees (PLAN §3.2), bound to one role's workspace and sandbox.

The role's write capability lives in the ``Workspace``, fixed when it is built; no tool
input can name or change it. Read-only tools may run concurrently within a turn; writes
and commands run one at a time, in the order the model asked for them. A refused call
(``ToolError``) comes back to the model as an error result and the run continues.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from msa.llm.gateway import Tool, ToolOutput
from msa.tools.sandbox import CommandName, LocalSandbox
from msa.tools.workspace import Workspace

Line = Annotated[int, Field(ge=1)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFile(_Input):
    path: str = Field(description="Path relative to the repository root.")
    start_line: Line | None = Field(description="First line to return (1-based), or null.")
    end_line: Line | None = Field(description="Last line to return (inclusive), or null.")


class Search(_Input):
    pattern: str = Field(description="A regular expression (Rust regex syntax).")
    glob: str | None = Field(description='Limit to paths matching this glob, e.g. "src/**/*.py".')


class Edit(_Input):
    path: str = Field(description="Path relative to the repository root.")
    old: str = Field(description="Exact text to replace; it must occur exactly once.")
    new: str = Field(description="The replacement text.")
    expected_hash: str = Field(description="content_hash from the read_file that showed it.")


class CreateFile(_Input):
    path: str = Field(description="Path relative to the repository root; must not exist.")
    content: str


class Run(_Input):
    command: CommandName
    paths: list[str] = Field(description="Paths to pass, relative to the root; [] for all.")
    keyword: str | None = Field(description="pytest -k expression, or null.")


class GitDiff(_Input):
    base: str | None = Field(description="Revision to compare against; null for HEAD.")


def toolset(workspace: Workspace, sandbox: LocalSandbox) -> tuple[Tool[Any], ...]:
    async def read_file(args: ReadFile) -> ToolOutput:
        view = await asyncio.to_thread(
            workspace.read_file, args.path, args.start_line, args.end_line
        )
        return ToolOutput(view.render())

    async def search(args: Search) -> ToolOutput:
        return ToolOutput((await sandbox.search(args.pattern, args.glob)).render())

    async def edit(args: Edit) -> ToolOutput:
        done = await asyncio.to_thread(
            workspace.edit, args.path, args.old, args.new, args.expected_hash
        )
        return ToolOutput(done.render())

    async def create_file(args: CreateFile) -> ToolOutput:
        done = await asyncio.to_thread(workspace.create_file, args.path, args.content)
        return ToolOutput(done.render())

    async def run(args: Run) -> ToolOutput:
        done = await sandbox.run(args.command, args.paths, args.keyword)
        return ToolOutput(done.render(), is_error=done.exit_code != 0)

    async def git_diff(args: GitDiff) -> ToolOutput:
        return ToolOutput((await sandbox.git_diff(args.base)).render())

    return (
        Tool(
            "read_file",
            "Read a text file. Lines come back numbered (the numbers are not part of the "
            "file) with the file's content_hash, which edit requires.",
            ReadFile,
            read_file,
            parallel_safe=True,
        ),
        Tool(
            "search",
            "Search file contents with ripgrep (respects .gitignore, skips binaries).",
            Search,
            search,
            parallel_safe=True,
        ),
        Tool(
            "git_diff",
            "Show uncommitted changes against a revision, and untracked files.",
            GitDiff,
            git_diff,
            parallel_safe=True,
        ),
        Tool(
            "edit",
            "Replace one exact occurrence of old with new. Fails if the file changed since "
            "the read that produced expected_hash, or if you may not write the path.",
            Edit,
            edit,
        ),
        Tool(
            "create_file",
            "Create a new file (and its directories). Fails if it exists or you may not "
            "write the path.",
            CreateFile,
            create_file,
        ),
        Tool(
            "run",
            "Run an allowlisted command (pytest, ruff_check, ruff_format_check, mypy) with "
            "a timeout and no network.",
            Run,
            run,
        ),
    )
