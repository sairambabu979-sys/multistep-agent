"""FINAL_VERIFICATION and publishing (ADR-015): the Release Verifier, then a person.

``verify_release(tree)`` builds the patch from the base tree once more and checks, without
a model:

- **There is a change**: an empty patch releases nothing.
- **It stays in scope**: every changed path is one the Coder or the Tester may write, and
  none is protected. The tool layer already enforced this on every write; this checks the
  result, whatever produced it.
- **It holds no secret**: the whole patch is scanned again (``msa.secrets``).
- **Every gate's record names this tree**: static analysis, unit tests, integration tests
  and the security review each recorded a pass for exactly the tree being released. The
  core already refuses to get here otherwise; the run files must agree with the journal.

Then it writes the pull request: ``pr.md`` (title, description, the evidence of every gate,
the review, the dependency changes, follow-ups, how to approve) and the bundle (the same,
as JSON, with the patch), stored as an artifact. ``ReleaseVerified`` names the bundle and a
person is asked. No agent can approve: the core refuses any ``agent:`` principal.

``publish_release`` runs only after a person approved. It commits the tree onto the base
commit, with the description as its message and ``Approved-by:`` naming the person, to a
local branch ``<[release].branch_prefix><run>``. Nothing is pushed: the person who approved
pushes, and opens the PR from ``pr.md``. Published again after a crash, it finds the branch
already holding this tree on this base and reuses that commit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from msa.agents.schemas import Plan
from msa.config import CapabilitiesConfig, Role
from msa.core.effects import Effect, PublishRelease, VerifyRelease
from msa.core.events import (
    AnyEvent,
    ReleasePublished,
    ReleaseVerificationFailed,
    ReleaseVerified,
)
from msa.core.ids import TreeHash
from msa.core.values import Severity
from msa.runtime import integration, security
from msa.runtime.artifacts import PLAN_FILE, RunFiles
from msa.runtime.context import RunContext
from msa.runtime.intake import base_commit
from msa.runtime.review import FINDINGS, ROUNDS, RULINGS
from msa.runtime.worktree import GitError, git
from msa.secrets import scan_lines
from msa.tools.capabilities import WritePolicy

PR_FILE = "pr.md"
TITLE_LIMIT = 72
AUTHOR = {
    "GIT_AUTHOR_NAME": "AEGIS",
    "GIT_AUTHOR_EMAIL": "aegis@localhost",
    "GIT_COMMITTER_NAME": "AEGIS",
    "GIT_COMMITTER_EMAIL": "aegis@localhost",
}


CLAUSE_ENDS = (": ", ". ", "; ")


def title_of(task: str) -> str:
    """The PR title, in the task's own words: its first line, or the longest leading clause
    of it that fits; cut at a word only when no clause fits."""
    first = next((line.strip() for line in task.splitlines() if line.strip()), "Change")
    first = first.lstrip("#").strip() or "Change"
    if len(first) <= TITLE_LIMIT:
        return first
    ends = [i for i in range(len(first)) if first.startswith(CLAUSE_ENDS, i)]
    if fitting := [i for i in ends if i <= TITLE_LIMIT]:
        return first[: max(fitting)]
    cut = first[: TITLE_LIMIT - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,") + "…"


def out_of_scope(paths: Sequence[str], capabilities: CapabilitiesConfig) -> list[str]:
    writers = [WritePolicy(role, capabilities) for role in (Role.CODER, Role.TESTER)]
    return [p for p in paths if not any(w.allows(p) for w in writers)]


def _passed(record: Mapping[str, Any]) -> bool:
    if "passed" in record:  # static analysis records a flag
        return bool(record["passed"])
    return record.get("verdict") == "passed"


GATE_RECORDS = (
    ("Static analysis", "verifications.jsonl"),
    ("Unit tests", "tests.jsonl"),
    ("Integration tests", integration.RECORDS),
    ("Security review", security.RECORDS),
)


def gate_evidence(files: RunFiles, tree: TreeHash) -> tuple[list[dict[str, Any]], list[str]]:
    """The passing record of each gate for ``tree``, and what is missing."""
    found, missing = [], []
    for name, records in GATE_RECORDS:
        mine = [r for r in files.records(records) if r.get("tree") == tree and _passed(r)]
        if mine:
            found.append({"gate": name, **mine[-1]})
        else:
            missing.append(f"{name}: no passing record for tree {tree}")
    return found, missing


def _evidence_rows(evidence: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = ["| gate | result | report |", "|---|---|---|"]
    for record in evidence:
        result = "passed"
        if record["gate"] == "Integration tests" and not record.get("ran", True):
            result = "nothing to run (no integration suite)"
        elif record["gate"] == "Integration tests":
            result = "passed, " + ("without network" if record.get("isolated") else "with network")
        elif record["gate"] == "Security review" and record.get("advisory"):
            result = f"passed, {record['advisory']} advisory finding(s)"
        rows.append(f"| {record['gate']} | {result} | `{record['report']}` |")
    return rows


def _sequence(record: Mapping[str, Any]) -> int:
    """The journal position of the effect that wrote ``record`` (ids are ``run:seq:index``)."""
    _, seq, _ = str(record["effect"]).rsplit(":", 2)
    return int(seq)


def _failure(gate: str, record: Mapping[str, Any]) -> str:
    if gate == "Static analysis":
        return ", ".join(f"`{name}`" for name in record.get("failed", [])) or "failed"
    if gate == "Security review":
        return f"{record.get('blocking', 0)} blocking finding(s)"
    return str(record.get("kind") or "failed")


def history_rows(files: RunFiles) -> list[str]:
    """Every gate failure in this run, in the order it happened, whichever tree it was on:
    what the gates caught on the way to the tree delivered here."""
    failed = [
        (_sequence(r), name, r)
        for name, records in GATE_RECORDS
        for r in files.records(records)
        if not _passed(r)
    ]
    if not failed:
        return []
    rows = ["| gate | tree | what failed | report |", "|---|---|---|---|"]
    for _, name, record in sorted(failed, key=lambda item: item[0]):
        tree_id = str(record.get("tree", ""))[:12]
        rows.append(f"| {name} | `{tree_id}` | {_failure(name, record)} | `{record['report']}` |")
    return rows


def _review_lines(files: RunFiles) -> tuple[list[str], list[str]]:
    findings = files.records(FINDINGS)
    admitted = [f for f in findings if f.get("admitted")]
    rulings = {r["finding"]: r for r in files.records(RULINGS) if r.get("verdict")}
    negotiated = {r["finding"]: r["verdict"] for r in files.records(ROUNDS) if r.get("verdict")}
    if not findings:
        return ["No findings were raised."], []
    lines = [f"{len(findings)} finding(s) raised, {len(admitted)} admitted.", ""]
    deferred = []
    for record in admitted:
        finding = record["finding"]
        ruling = rulings.get(record["id"])
        if ruling:
            outcome = f"{ruling['verdict']} ({ruling['rung']})"
        elif verdict := negotiated.get(record["id"]):
            outcome = f"{verdict} in negotiation"
        elif not Severity(record["severity"]).is_blocking:
            outcome = "advisory: it does not block, so it was not negotiated"
        else:
            outcome = "closed with no verdict recorded"
        title = " ".join(str(finding.get("claim") or "finding").split()).rstrip(".")
        lines.append(f"- `{record['id']}` [{record['severity']}] {title}: {outcome}")
        if ruling and ruling["verdict"] == "deferred":
            deferred.append(f"- `{record['id']}` {title}")
    return lines, deferred


class ReleaseExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, VerifyRelease):
            raise TypeError(f"the release verifier cannot execute {command.kind}")
        ctx = self.context
        await ctx.worktree.prune()
        current = await ctx.worktree.snapshot()
        if current != command.tree:
            raise RuntimeError(
                f"the worktree holds tree {current}, but release verification was asked for "
                f"{command.tree}"
            )
        tree = command.tree
        base = await ctx.worktree.base_tree()
        patch = await ctx.worktree.diff(base, tree)
        changed = await ctx.worktree.changed_files(base, tree, deleted=True)
        problems: list[str] = []
        if not patch.strip():
            problems.append("The change is empty: there is nothing to release.")
        if stray := out_of_scope(changed, ctx.config.capabilities):
            listed = ", ".join(f"`{p}`" for p in stray)
            problems.append(f"Paths no role may write were changed: {listed}.")
        added = security.added_lines(await ctx.worktree.changes(base, tree))
        if leaks := [
            m.render(path) for path, lines in sorted(added.items()) for m in scan_lines(lines)
        ]:
            problems.append("The patch holds secrets: " + "; ".join(leaks) + ".")
        evidence, missing = gate_evidence(ctx.files, tree)
        problems += [f"Missing evidence. {m}." for m in missing]

        if problems:
            report = "\n".join(
                [
                    f"# Release verification of tree {tree}: FAILED",
                    "",
                    *(f"- {p}" for p in problems),
                ]
            )
            digest = ctx.files.put(report)
            ctx.files.record(
                "releases.jsonl",
                {"effect": effect.id, "tree": tree, "verdict": "failed", "report": digest},
            )
            return ReleaseVerificationFailed(at=now, effect_id=effect.id, tree=tree, report=digest)

        commit = base_commit(ctx.files)
        title = title_of(ctx.task)
        body = self._description(tree, commit, changed, evidence)
        bundle = {
            "run": ctx.run_id,
            "title": title,
            "body": body,
            "base_commit": commit,
            "base_tree": base,
            "tree": tree,
            "changed": changed,
            "patch": ctx.files.put(patch),
            "evidence": evidence,
        }
        ctx.files.write(PR_FILE, f"# {title}\n\n{body}")
        digest = ctx.files.put(json.dumps(bundle, indent=2, sort_keys=True))
        ctx.files.record(
            "releases.jsonl",
            {"effect": effect.id, "tree": tree, "verdict": "passed", "bundle": digest},
        )
        return ReleaseVerified(at=now, effect_id=effect.id, tree=tree, bundle=digest)

    def _description(
        self,
        tree: TreeHash,
        commit: str,
        changed: Sequence[str],
        evidence: Sequence[Mapping[str, Any]],
    ) -> str:
        ctx = self.context
        stored = ctx.files.read(PLAN_FILE)
        summary = Plan.model_validate_json(stored).summary.strip() if stored else "(no plan)"
        review, deferred = _review_lines(ctx.files)
        history = history_rows(ctx.files)
        manifests = [p for p in changed if security.is_manifest(p)]
        lines = [
            "## Summary",
            "",
            summary,
            "",
            "## Task",
            "",
            *(f"> {line}" if line else ">" for line in ctx.task.strip().splitlines()),
            "",
            f"## Changes ({len(changed)} file(s))",
            "",
            *(f"- `{p}`" for p in changed),
            "",
            "## Evidence",
            "",
            f"Every gate passed on exactly the tree delivered here, `{tree}`.",
            "",
            *_evidence_rows(evidence),
            "",
        ]
        if history:
            lines += [
                "## Caught along the way",
                "",
                "These checks failed earlier in this run. Each gate passed again on the tree "
                "delivered here (Evidence, above).",
                "",
                *history,
                "",
            ]
        lines += [
            "## Review",
            "",
            *review,
            "",
        ]
        if manifests:
            lines += [
                "## Dependency changes",
                "",
                *(f"- `{p}`" for p in manifests),
                "",
            ]
        if deferred:
            lines += ["## Follow-ups (deferred findings)", "", *deferred, ""]
        lines += [
            "## Audit",
            "",
            f"- Run: `{ctx.run_id}` (journal: `msa inspect {ctx.run_id}`)",
            f"- Base commit: `{commit}`",
            f"- Head tree: `{tree}`",
            f"- Approve: `msa approve {ctx.run_id}`; send back: "
            f'`msa approve {ctx.run_id} --reject "why"`',
            "",
        ]
        return "\n".join(lines)


class PublishExecutor:
    def __init__(self, context: RunContext) -> None:
        self.context = context

    async def execute(self, effect: Effect, *, now: datetime) -> AnyEvent:
        command = effect.command
        if not isinstance(command, PublishRelease):
            raise TypeError(f"publishing cannot execute {command.kind}")
        ctx = self.context
        bundle = json.loads(ctx.files.get(command.bundle))
        if bundle["tree"] != command.tree:
            raise RuntimeError(f"bundle {command.bundle} is for tree {bundle['tree']}")
        parent = bundle["base_commit"]
        branch = f"{ctx.config.release.branch_prefix}{ctx.run_id}"
        ref = f"refs/heads/{branch}"
        repo = ctx.worktree.repo
        commit = await self._existing(ref, command.tree, parent)
        if commit is None:
            message = (
                f"{bundle['title']}\n\n{bundle['body']}\n"
                f"Approved-by: {command.approved_by}\nAEGIS-Run: {ctx.run_id}\n"
            )
            commit = (
                await git(
                    repo, "commit-tree", command.tree, "-p", parent, "-m", message, env=AUTHOR
                )
            ).strip()
            # Create only: never move a branch someone else owns.
            await git(repo, "update-ref", ref, commit, "0" * 40)
        reference = f"{branch} at {commit}"
        ctx.files.record(
            "releases.jsonl",
            {
                "effect": effect.id,
                "tree": command.tree,
                "verdict": "published",
                "branch": branch,
                "commit": commit,
                "approved_by": command.approved_by,
            },
        )
        return ReleasePublished(at=now, effect_id=effect.id, tree=command.tree, reference=reference)

    async def _existing(self, ref: str, tree: TreeHash, parent: str) -> str | None:
        """The commit ``ref`` already holds, if it is this release; raise if it is another."""
        repo = self.context.worktree.repo
        try:
            commit = (await git(repo, "rev-parse", "--verify", "--quiet", ref)).strip()
        except GitError:
            return None
        shape = (await git(repo, "show", "-s", "--format=%T %P", commit)).split()
        if shape != [tree, parent]:
            raise RuntimeError(
                f"{ref} already exists and holds another change ({commit}); delete or rename "
                "it, then resume"
            )
        return commit
