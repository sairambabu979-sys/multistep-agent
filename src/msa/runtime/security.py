"""SECURITY_REVIEW (ADR-015): what the change adds, held to rules no one can negotiate.

``run_security_review(tree)`` looks only at what the run changed from the base tree, so a
repository's existing debt never blocks a run, and nothing the run adds escapes:

1. **Secrets** in any added line, in any file, the Tester's included (``msa.tools.secrets``).
   Always blocking.
2. **Insecure code** in the added lines of Python files, found in the syntax tree, never by
   matching text: ``eval``/``exec``, shell commands (``shell=True``, ``os.system``,
   ``os.popen``), unpickling, ``yaml.load`` without a safe loader, TLS verification turned
   off. These block. Weak hashes without ``usedforsecurity=False``, ``tempfile.mktemp``,
   world-writable modes and ``debug=True`` are reported but do not block.
3. **Dependencies.** A changed manifest (``pyproject.toml``, ``requirements*.txt``, lock
   files, ``package.json``...) is always listed, so the person approving sees it. With
   ``[security].forbid_new_dependencies`` it blocks; with ``[security].audit_command`` the
   audit runs, and a non-zero exit blocks.

A blocking finding never reaches negotiation: the core sends it straight to rework. It goes
to whoever can fix all of them: the Tester when every one is in its files, which the Coder
may not edit; otherwise the Coder (the Tester's come back on the next pass, since every
gate re-runs after a revision).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from msa.config import Role
from msa.core.effects import Effect, RunSecurityReview
from msa.core.events import AnyEvent, SecurityReviewFailed, SecurityReviewPassed
from msa.core.values import Owner
from msa.runtime.context import RunContext
from msa.runtime.verify import removed_section
from msa.tools.secrets import scan_lines

type Severity = Literal["critical", "high", "medium"]
BLOCKING: frozenset[str] = frozenset({"critical", "high"})
RECORDS = "security.jsonl"
MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "requirements*.in",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True, slots=True)
class Issue:
    path: str
    line: int
    severity: Severity
    rule: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING

    def render(self) -> str:
        return f"- **{self.severity}** `{self.path}:{self.line}` {self.rule}: {self.detail}"


# ---------- what the change adds


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """The lines a ``git diff`` adds, with their new-side numbers, per file."""
    added: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    number = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None  # /dev/null: deleted
            continue
        if line.startswith(("--- ", "diff --git", "index ")):
            continue
        if match := _HUNK.match(line):
            number = int(match[1])
            continue
        if path is None:
            continue
        if line.startswith("+"):
            added.setdefault(path, []).append((number, line[1:]))
            number += 1
        elif line.startswith(" "):
            number += 1
    return added


def secret_issues(added: Mapping[str, Sequence[tuple[int, str]]]) -> list[Issue]:
    return [
        Issue(path, m.line, "critical", "secret", f"{m.rule} ({m.redacted}); never commit one")
        for path, lines in sorted(added.items())
        for m in scan_lines(lines)
    ]


# ---------- insecure code, by the syntax tree


def _imports(tree: ast.Module) -> dict[str, str]:
    """Local name -> the dotted name it stands for, from the module's imports."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr, imports: Mapping[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = imports.get(node.id, node.id)
    return ".".join([head, *reversed(parts)])


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


_SHELL = {"os.system", "os.popen", "commands.getoutput", "subprocess.getoutput"}
_SUBPROCESS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
}
_UNPICKLE = {"pickle.load", "pickle.loads", "cPickle.load", "cPickle.loads", "dill.loads"}
_SAFE_YAML = {"SafeLoader", "CSafeLoader", "BaseLoader"}
_WEAK_HASH = {"hashlib.md5", "hashlib.sha1"}
_HTTP = ("requests.", "httpx.")


def _call_issues(call: ast.Call, name: str) -> Iterable[tuple[Severity, str, str]]:  # noqa: PLR0912 (one case per rule)
    if name in {"eval", "exec"}:
        yield "high", "code execution", f"`{name}()` runs arbitrary code"
    elif name in _SHELL:
        yield "high", "shell command", f"`{name}()` runs its argument through a shell"
    elif name in _SUBPROCESS and _is_const(_keyword(call, "shell"), True):
        yield "high", "shell command", f"`{name}(shell=True)`: pass an argv list instead"
    elif name in _UNPICKLE:
        yield "high", "unsafe deserialization", f"`{name}()` can run code from its input"
    elif name == "yaml.load":
        loader = _keyword(call, "Loader") or (call.args[1] if len(call.args) > 1 else None)
        loaded = loader.attr if isinstance(loader, ast.Attribute) else None
        if loaded is None and isinstance(loader, ast.Name):
            loaded = loader.id
        if loaded not in _SAFE_YAML:
            yield "high", "unsafe deserialization", "`yaml.load()` without a safe Loader"
    elif name == "ssl._create_unverified_context":
        yield "high", "TLS verification off", f"`{name}()` accepts any certificate"
    elif name.startswith(_HTTP) and _is_const(_keyword(call, "verify"), False):
        yield "high", "TLS verification off", f"`{name}(verify=False)` accepts any certificate"
    elif name in _WEAK_HASH and not _is_const(_keyword(call, "usedforsecurity"), False):
        yield (
            "medium",
            "weak hash",
            f"`{name}()`: use sha256, or say `usedforsecurity=False` for a checksum",
        )
    elif name == "tempfile.mktemp":
        yield (
            "medium",
            "temp file race",
            "`tempfile.mktemp()`: use `mkstemp` or `NamedTemporaryFile`",
        )
    elif name in {"os.chmod", "os.fchmod"} and len(call.args) > 1:
        mode = call.args[1]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, int) and mode.value & 0o002:
            yield "medium", "world-writable", f"`{name}(..., {oct(mode.value)})`"
    if name.endswith(".run") and _is_const(_keyword(call, "debug"), True):
        yield "medium", "debug mode", f"`{name}(debug=True)` must not ship"


def code_issues(path: str, source: str, lines: Iterable[int]) -> list[Issue]:
    """Insecure calls on the given (added) lines of one Python file."""
    wanted = set(lines)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []  # STATIC_ANALYSIS reports syntax; nothing here to judge
    imports = _imports(tree)
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno not in wanted:
            continue
        name = _dotted(node.func, imports)
        if name is None:
            continue
        for severity, rule, detail in _call_issues(node, name):
            issues.append(Issue(path, node.lineno, severity, rule, detail))
    return issues


def is_manifest(path: str) -> bool:
    return any(fnmatchcase(PurePosixPath(path).name, pattern) for pattern in MANIFESTS)


def owner_of(issues: Sequence[Issue], tester: Sequence[str]) -> Owner:
    def testers(path: str) -> bool:
        return any(path == d or path.startswith(f"{d}/") for d in tester)

    blocking = [i for i in issues if i.blocking]
    return Owner.TESTER if blocking and all(testers(i.path) for i in blocking) else Owner.CODER


# ---------- the executor


class SecurityExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, RunSecurityReview):
            raise TypeError(f"the security review cannot execute {command.kind}")
        ctx = self.context
        removed = await ctx.worktree.prune()
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but the security review was asked for "
                f"{command.tree}"
            )
        tree = command.tree
        base = await ctx.worktree.base_tree()
        added = added_lines(await ctx.worktree.changes(base, tree))
        issues = secret_issues(added)
        for path, lines in sorted(added.items()):
            if path.endswith(".py"):
                source = await ctx.worktree.read(tree, path) or ""
                issues += code_issues(path, source, (n for n, _ in lines))
        changed = await ctx.worktree.changed_files(base, tree, deleted=True)
        manifests = [p for p in changed if is_manifest(p)]
        sections, dependency_issues = await self._dependencies(manifests)
        issues += dependency_issues

        blocking = [i for i in issues if i.blocking]
        verdict = "FAILED" if blocking else "passed"
        out = [f"# Security review of tree {tree}: {verdict}", ""]
        scope = sum(len(v) for v in added.values())
        out += [f"{scope} added line(s) in {len(added)} file(s) checked.", ""]
        if issues:
            out += ["## Findings", "", *(i.render() for i in issues), ""]
        else:
            out += ["No secrets and no insecure calls in the added lines.", ""]
        out += sections
        if removed:
            out.append(removed_section(removed))
        report = "\n".join(out)
        digest = ctx.files.put(report)
        tester = ctx.config.capabilities.owned_directories(Role.TESTER)
        owner = owner_of(issues, tester)
        ctx.files.record(
            RECORDS,
            {
                "effect": effect.id,
                "tree": tree,
                "verdict": "failed" if blocking else "passed",
                "blocking": len(blocking),
                "advisory": len(issues) - len(blocking),
                "manifests": manifests,
                "owner": owner.value if blocking else None,
                "report": digest,
            },
        )
        if blocking:
            return SecurityReviewFailed(
                at=now, effect_id=effect.id, tree=tree, report=digest, owner=owner
            )
        return SecurityReviewPassed(at=now, effect_id=effect.id, tree=tree, report=digest)

    async def _dependencies(self, manifests: Sequence[str]) -> tuple[list[str], list[Issue]]:
        if not manifests:
            return [], []
        policy = self.context.config.security
        listed = "\n".join(f"- `{p}`" for p in manifests)
        sections = [
            "## Dependency manifests changed\n\nA person approving this change should see "
            f"these:\n\n{listed}\n"
        ]
        issues = []
        if policy.forbid_new_dependencies:
            issues += [
                Issue(p, 1, "high", "dependency change", "forbidden by [security]")
                for p in manifests
            ]
        if not policy.audit_command:
            sections.append("No dependency audit is configured ([security].audit_command).\n")
            return sections, issues
        sandbox = self.context.sandbox(Role.CODER)
        argv = list(policy.audit_command)
        done = await sandbox.execute(argv, shown=" ".join(argv), network=True)
        sections.append(f"## Dependency audit\n\n{done.render()}\n")
        if done.exit_code != 0:
            issues.append(Issue(manifests[0], 1, "high", "dependency audit", f"`{argv[0]}` failed"))
        return sections, issues
