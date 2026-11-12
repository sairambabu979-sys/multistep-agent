"""Machine checks for preferences (PLAN §5.4): a linter, not an LLM, wherever one can.

Each enabled preference with a ``check`` is run over the files it applies to:

- ``ruff``: one ``ruff check --isolated --select <codes>`` over every applicable file.
  ``--isolated`` means the project's ruff config cannot quietly disable the owner's rule;
  an inline ``# noqa`` still can, deliberately and visibly.
- ``regex``: ripgrep, so a pattern cannot hang the run, over the files in the scope.

Both run in the sandbox (ADR-009). Findings are sorted, so a report is reproducible.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from msa.skills.preferences import Preference, Resolved
from msa.skills.preferences.schema import RegexCheck, RuffCheck
from msa.tools.sandbox import LocalSandbox


class CheckError(RuntimeError):
    """A checker could not run (not installed, crashed): unlike a finding, not the code's fault."""


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
    try:
        # Fail closed: ruff --output-format=json always prints an array, even "[]". Empty
        # output means it did not run (a broken launcher can exit 1 too), not "all clear".
        diagnostics = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise CheckError(f"ruff did not produce a report: {done.render()}") from None
    if not isinstance(diagnostics, list):
        raise CheckError(f"ruff did not produce a report: {done.render()}")
    root = str(sandbox.root.path)
    found = []
    for item in diagnostics:
        path = str(item["filename"]).removeprefix(root + "/")
        for p in preferences:
            assert isinstance(p.check, RuffCheck)
            if item["code"].startswith(p.check.rule) and p.scope.applies_to(path):
                found.append(
                    CheckFinding(
                        path,
                        int(item["location"]["row"]),
                        p.id,
                        p.enforcement,
                        f"{item['code']} {item['message']}",
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
    found = []
    for line in done.stdout.splitlines():
        event = json.loads(line)
        if event.get("type") != "match":
            continue
        data = event["data"]
        path = data["path"].get("text")
        if path is None:  # pragma: no cover - a non-UTF-8 name; rg sends bytes
            continue
        text = data["lines"].get("text", "").rstrip("\n")
        found.append(
            CheckFinding(
                path,
                int(data["line_number"]),
                preference.id,
                preference.enforcement,
                f"matches {check.pattern!r}: {text[:120]}",
            )
        )
    return found
