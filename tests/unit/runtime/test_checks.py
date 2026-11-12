"""Machine checks for preferences, run in the sandbox (PLAN §5.4)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from msa.runtime.checks import CheckError, CheckFinding, run_checks
from msa.skills.preferences import Resolved, resolve, store
from msa.skills.preferences.resolve import Layer
from msa.tools.paths import Root
from msa.tools.sandbox import Completed, LocalSandbox
from tests.support.tools import sandbox_config

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")

APP = 'def f():\n    try:\n        pass\n    except:\n        breakpoint()\n    print("x")\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(APP)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text('print("fine outside src")\n')
    (tmp_path / "notes.md").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> b\nTitle\n=======\n"
    )
    return tmp_path


def sandbox(repo: Path, **overrides: object) -> LocalSandbox:
    overrides.setdefault("launcher", (sys.executable, "-m"))
    return LocalSandbox(Root.at(repo), sandbox_config(**overrides))


def check(
    repo: Path, resolved: Resolved, files: list[str], **overrides: object
) -> list[CheckFinding]:
    return asyncio.run(run_checks(sandbox(repo, **overrides), resolved, files))


def test_the_defaults_find_each_violation_where_its_scope_applies(repo: Path) -> None:
    files = ["src/app.py", "scripts/tool.py", "notes.md"]
    found = check(repo, resolve([store.builtin_layer()]), files)
    assert [(f.path, f.line, f.preference, f.enforcement) for f in found] == [
        ("notes.md", 1, "general.no-merge-markers", "hard"),
        ("notes.md", 5, "general.no-merge-markers", "hard"),
        ("src/app.py", 4, "py.errors.no-bare-except", "hard"),
        ("src/app.py", 5, "py.debug.no-breakpoints", "hard"),
        ("src/app.py", 6, "py.output.no-print", "soft"),  # scripts/ is outside its src/** scope
    ]
    assert found[2].render() == (
        "src/app.py:4: py.errors.no-bare-except (hard): E722 Do not use bare `except`"
    )


def test_switched_off_and_unchecked_preferences_run_nothing(repo: Path) -> None:
    off = Layer(
        "run",
        "--pref",
        {
            p: {"enforcement": "off"}
            for p in (
                "general.no-merge-markers",
                "py.errors.no-bare-except",
                "py.debug.no-breakpoints",
                "py.output.no-print",
            )
        },
    )
    assert check(repo, resolve([store.builtin_layer(), off]), ["src/app.py", "notes.md"]) == []
    assert check(repo, resolve([store.builtin_layer()]), []) == []


@pytest.mark.parametrize("selector", ["E722", "E7"])
def test_ruff_exact_and_prefix_selectors_attribute_findings(repo: Path, selector: str) -> None:
    resolved = resolve(
        [
            Layer(
                "repo",
                "repo",
                {
                    "team.errors": {
                        "rule": "No bare except.",
                        "enforcement": "hard",
                        "check": {"kind": "ruff", "rule": selector},
                    }
                },
            )
        ]
    )
    found = check(repo, resolved, ["src/app.py"])
    assert [(f.preference, f.line, f.enforcement) for f in found] == [("team.errors", 4, "hard")]
    assert found[0].message.startswith("E722 ")
    assert check(repo, resolved, ["scripts/tool.py"]) == []


@pytest.mark.parametrize("null_code", [False, True])
def test_syntax_error_diagnostics_do_not_match_prefix_preferences(
    repo: Path, null_code: bool
) -> None:
    (repo / "src/app.py").write_text("def broken(:\n")
    resolved = resolve(
        [
            Layer(
                "repo",
                "repo",
                {
                    "team.errors": {
                        "rule": "No bare except.",
                        "enforcement": "hard",
                        "check": {"kind": "ruff", "rule": "E7"},
                    }
                },
            )
        ]
    )
    if null_code:
        diagnostic = {
            "code": None,
            "filename": str(repo / "src/app.py"),
            "location": {"row": 1},
            "message": "SyntaxError: invalid syntax",
        }
        launcher = (sys.executable, "-c", f"print({json.dumps([diagnostic])!r})")
        assert check(repo, resolved, ["src/app.py"], launcher=launcher) == []
    else:
        assert check(repo, resolved, ["src/app.py"]) == []


def test_an_unknown_ruff_selector_fails_closed(repo: Path) -> None:
    resolved = resolve(
        [
            Layer(
                "repo",
                "repo",
                {
                    "team.errors": {
                        "rule": "No bare except.",
                        "enforcement": "hard",
                        "check": {"kind": "ruff", "rule": "ZZ999"},
                    }
                },
            )
        ]
    )
    with pytest.raises(CheckError, match="ruff failed"):
        check(repo, resolved, ["src/app.py"])


def test_a_checker_that_does_not_run_fails_closed(repo: Path) -> None:
    """A launcher that exits 1 with no report must not read as 'no violations'."""
    with pytest.raises(CheckError, match="ruff did not produce a report"):
        check(repo, resolve([store.builtin_layer()]), ["src/app.py"], launcher=("false",))
    with pytest.raises(CheckError, match="ruff failed"):
        check(
            repo,
            resolve([store.builtin_layer()]),
            ["src/app.py"],
            launcher=(sys.executable, "-c", "raise SystemExit(2)"),
        )


class FailingRg(LocalSandbox):
    async def execute(self, argv: Sequence[str], **kwargs: Any) -> Completed:
        if argv[0] == "rg":
            return Completed("rg", 2, 0.0, "", "rg: something broke", 0)
        return await super().execute(argv, **kwargs)


def test_a_failing_ripgrep_is_a_check_error(repo: Path) -> None:
    box = FailingRg(Root.at(repo), sandbox_config(launcher=(sys.executable, "-m")))
    with pytest.raises(CheckError, match="rg failed: rg: something broke"):
        asyncio.run(run_checks(box, resolve([store.builtin_layer()]), ["notes.md"]))


def test_a_ruff_report_that_is_not_a_list_is_a_check_error(repo: Path) -> None:
    printer = (sys.executable, "-c", "print('{}')")  # the args after it are ignored
    with pytest.raises(CheckError, match="ruff did not produce a report"):
        check(repo, resolve([store.builtin_layer()]), ["src/app.py"], launcher=printer)


class ReportSandbox(LocalSandbox):
    def __init__(self, repo: Path, completed: Completed) -> None:
        super().__init__(Root.at(repo), sandbox_config())
        self.completed = completed

    async def execute(self, argv: Sequence[str], **kwargs: Any) -> Completed:
        return self.completed


def report(repo: Path, kind: str, completed: Completed) -> list[CheckFinding]:
    config = (
        {"kind": "ruff", "rule": "T201"} if kind == "ruff" else {"kind": "regex", "pattern": "TODO"}
    )
    layer = Layer.from_document(
        "repo",
        "report.yaml",
        {
            "version": 1,
            "preferences": [
                {
                    "id": "test.report",
                    "rule": "No debug output.",
                    "enforcement": "hard",
                    "check": config,
                }
            ],
        },
    )
    return asyncio.run(run_checks(ReportSandbox(repo, completed), resolve([layer]), ["src/app.py"]))


@pytest.mark.parametrize("tool", ["ruff", "rg"])
def test_elision_is_rejected_even_when_the_remaining_report_is_valid_json(
    repo: Path, tool: str
) -> None:
    stdout = "[]" if tool == "ruff" else '{"type":"summary","data":{}}'
    with pytest.raises(CheckError, match=f"{tool} report is incomplete: output was elided"):
        report(repo, tool, Completed(tool, 0, 0.0, stdout, "", 100))


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "[",
        "{}",
        "null",
        "[null]",
        "[{}]",
        '[{"filename":42}]',
        '[{"filename":"src/app.py","code":"T201","message":"print","location":{"row":[]}}]',
    ],
)
def test_malformed_ruff_reports_raise_check_errors(repo: Path, stdout: str) -> None:
    with pytest.raises(CheckError, match="ruff did not produce a report"):
        report(repo, "ruff", Completed("ruff", 0, 0.0, stdout, "", 0))


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "{",
        "[]",
        "null",
        "{}",
        '{"type":"match","data":{}}',
        '{"type":"unknown","data":{}}',
        '{"type":"begin","data":{}}',
        '{"type":"summary","data":{}}\n{"type":"end","data":{}}',
    ],
)
def test_malformed_or_truncated_rg_reports_raise_check_errors(repo: Path, stdout: str) -> None:
    with pytest.raises(CheckError, match="rg did not produce a complete, valid JSON report"):
        report(repo, "rg", Completed("rg", 0, 0.0, stdout, "", 0))


@pytest.mark.parametrize(
    "data",
    [
        {"path": {}, "lines": {"text": "TODO"}, "line_number": 1},
        {"path": {"text": "src/app.py"}, "lines": {}, "line_number": 1},
        {"path": {"text": "src/app.py"}, "lines": {"text": "TODO"}, "line_number": []},
    ],
)
def test_malformed_rg_matches_raise_check_errors(repo: Path, data: dict[str, Any]) -> None:
    stdout = json.dumps({"type": "match", "data": data}) + '\n{"type":"summary","data":{}}'
    with pytest.raises(CheckError, match="rg did not produce a complete, valid JSON report"):
        report(repo, "rg", Completed("rg", 0, 0.0, stdout, "", 0))


def test_rg_bytes_reports_remain_supported(repo: Path) -> None:
    stdout = (
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"bytes": "bm9uLXV0Zjg="},
                    "lines": {"text": "TODO"},
                    "line_number": 1,
                },
            }
        )
        + '\n{"type":"summary","data":{}}'
    )
    assert report(repo, "rg", Completed("rg", 0, 0.0, stdout, "", 0)) == []


@pytest.mark.parametrize("tool", ["ruff", "rg"])
def test_complete_empty_reports_have_no_findings(repo: Path, tool: str) -> None:
    stdout = "[]" if tool == "ruff" else '{"type":"summary","data":{}}'
    assert report(repo, tool, Completed(tool, 1, 0.0, stdout, "", 0)) == []


@pytest.mark.parametrize("tool", ["ruff", "rg"])
def test_timed_out_reports_fail_closed(repo: Path, tool: str) -> None:
    with pytest.raises(CheckError, match=f"(?s){tool} failed: .*timed out"):
        report(repo, tool, Completed(tool, None, 1.0, "", "", 0))


@pytest.mark.parametrize(
    ("tool", "path", "content"),
    [("ruff", "src/app.py", 'print("debug")\n'), ("rg", "notes.md", "<<<<<<< HEAD\n")],
)
def test_real_capped_reports_raise_check_errors(
    repo: Path, tool: str, path: str, content: str
) -> None:
    (repo / path).write_text(content * 100)
    with pytest.raises(CheckError, match=f"{tool} report is incomplete: output was elided"):
        check(repo, resolve([store.builtin_layer()]), [path], output_limit_bytes=1000)
