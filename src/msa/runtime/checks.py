"""Machine checks for preferences (PLAN §5.4): a linter, not an LLM, wherever one can.

Each enabled preference with a ``check`` is run over the files it applies to:

- ``ruff``: one ``ruff check --isolated --select <codes>`` over every applicable file.
  ``--isolated`` means the project's ruff config cannot quietly disable the owner's rule;
  an inline ``# noqa`` still can, deliberately and visibly.
- ``regex``: ripgrep, so a pattern cannot hang the run, over the files in the scope.

Both run in the sandbox (ADR-009). Findings are sorted, so a report is reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, PositiveInt, TypeAdapter, ValidationError

from msa.skills.preferences import Preference, Resolved
from msa.skills.preferences.schema import RegexCheck, RuffCheck
from msa.tools.sandbox import LocalSandbox


class CheckError(RuntimeError):
    """A checker could not run (not installed, crashed): unlike a finding, not the code's fault."""


class _Location(BaseModel):
    row: PositiveInt


class _RuffDiagnostic(BaseModel):
    code: str | None
    filename: str
    message: str
    location: _Location


_RUFF_REPORT = TypeAdapter(list[_RuffDiagnostic])


class _RgEvent(BaseModel):
    type: Literal["begin", "match", "end", "summary"]
    data: dict[str, Any]


class _RgMatch(BaseModel):
    path: dict[Literal["text", "bytes"], str]
    lines: dict[Literal["text", "bytes"], str]
    line_number: PositiveInt


@dataclass(frozen=True, slots=True, order=True)
class CheckFinding:
    path: str
    line: int
    preference: str
    enforcement: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.preference} ({self.enforcement}): {self.message}"


async def run_checks(
    sandbox: LocalSandbox, resolved: Resolved, files: Iterable[str]
) -> list[CheckFinding]:
    paths = sorted({sandbox.root.resolve(f).as_posix() for f in files})
    checked = [p for p in resolved.select(paths) if p.check is not None]
    findings: list[CheckFinding] = []
    ruff = [p for p in checked if isinstance(p.check, RuffCheck)]
    if ruff:
        findings += await _ruff(sandbox, ruff, paths)
    for preference in checked:
        if isinstance(preference.check, RegexCheck):
            findings += await _regex(sandbox, preference, preference.check, paths)
    return sorted(findings)


async def _ruff(
    sandbox: LocalSandbox, preferences: Sequence[Preference], paths: Sequence[str]
) -> list[CheckFinding]:
    # Every preference here was selected, so it applies to at least one of the paths.
    targets = sorted({f for p in preferences for f in paths if p.scope.applies_to(f)})
    codes = sorted({p.check.rule for p in preferences if isinstance(p.check, RuffCheck)})
    argv = [*sandbox.config.launcher, "ruff", "check", "--isolated", "--no-cache", "--no-fix"]
    argv += ["--output-format=json", "--select", ",".join(codes), "--", *targets]
    done = await sandbox.execute(argv, shown="ruff check", merge=False)
    if done.exit_code not in {0, 1}:  # 1: violations found
        raise CheckError(f"ruff failed: {done.stderr.strip() or done.render()}")
    if done.elided_bytes:
        raise CheckError("ruff report is incomplete: output was elided; check fewer files")
    try:
        # Fail closed: ruff --output-format=json always prints an array, even "[]". Empty
        # output means it did not run (a broken launcher can exit 1 too), not "all clear".
        diagnostics = _RUFF_REPORT.validate_json(done.stdout)
    except ValidationError:
        raise CheckError(f"ruff did not produce a report: {done.render()}") from None
    root = str(sandbox.root.path)
    found = []
    for item in diagnostics:
        path = item.filename.removeprefix(root + "/")
        for p in preferences:
            assert isinstance(p.check, RuffCheck)
            if (
                item.code is not None
                and item.code.startswith(p.check.rule)
                and p.scope.applies_to(path)
            ):
                found.append(
                    CheckFinding(
                        path,
                        item.location.row,
                        p.id,
                        p.enforcement,
                        f"{item.code} {item.message}",
                    )
                )
    return found


async def _regex(
    sandbox: LocalSandbox, preference: Preference, check: RegexCheck, paths: Sequence[str]
) -> list[CheckFinding]:
    targets = [f for f in paths if preference.scope.applies_to(f)]  # non-empty: selected
    argv = ["rg", "--json", "--no-config", "--regexp", check.pattern, "--", *targets]
    done = await sandbox.execute(argv, shown="rg", merge=False)
    if done.exit_code not in {0, 1}:  # 1: no match
        raise CheckError(f"rg failed: {done.stderr.strip() or done.render()}")
    if done.elided_bytes:
        raise CheckError("rg report is incomplete: output was elided; check fewer files")
    found = []
    complete = False
    try:
        for line in done.stdout.splitlines():
            event = _RgEvent.model_validate_json(line)
            if complete:
                raise ValueError("data after summary")
            complete = event.type == "summary"
            if event.type != "match":
                continue
            data = _RgMatch.model_validate(event.data)
            if not data.path or not data.lines:
                raise ValueError("missing match text or bytes")
            path = data.path.get("text")
            if path is None:  # a non-UTF-8 name; rg sends bytes
                continue
            text = data.lines.get("text", "").rstrip("\n")
            found.append(
                CheckFinding(
                    path,
                    data.line_number,
                    preference.id,
                    preference.enforcement,
                    f"matches {check.pattern!r}: {text[:120]}",
                )
            )
        if not complete:
            raise ValueError("missing summary")
    except ValueError:
        raise CheckError("rg did not produce a complete, valid JSON report") from None
    return found
