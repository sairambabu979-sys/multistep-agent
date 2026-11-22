"""The typed answers agents return (PLAN §3), validated through structured outputs.

Every model is strict-compatible (``msa.llm.schema``): closed, every field required, and
no constraint strict mode rejects. Pydantic still enforces the constraints client-side, so
an answer that breaks one fails the invocation instead of reaching the state machine.

The state machine never sees these. It sees a ``PlanRef`` (an id and the digest of the
stored plan), and the executors keep the full answers as run artifacts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from msa.core.values import Severity

Text = Annotated[str, Field(min_length=1)]


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanStep(_Answer):
    description: Text = Field(description="What to change and why, in one or two sentences.")
    files: list[str] = Field(
        description="Paths relative to the repository root that this step reads or writes."
    )


class Plan(_Answer):
    summary: Text = Field(description="The approach, in two or three sentences.")
    steps: Annotated[list[PlanStep], Field(min_length=1)] = Field(
        description="Ordered implementation steps; at least one."
    )
    acceptance_criteria: Annotated[list[Text], Field(min_length=1)] = Field(
        description=(
            "Observable behaviors a test can check, each phrased so it is false before the "
            "change and true after it."
        )
    )
    risks: list[str] = Field(
        description="What could go wrong or is ambiguous in the task; [] if nothing."
    )

    def render(self) -> str:
        """Markdown for the Coder's prompt and the run report. Byte-stable for one plan."""
        lines = ["## Plan", "", self.summary.strip(), "", "### Steps"]
        for number, step in enumerate(self.steps, 1):
            files = f" ({', '.join(step.files)})" if step.files else ""
            lines.append(f"{number}. {step.description.strip()}{files}")
        lines += ["", "### Acceptance criteria"]
        lines += [f"- {criterion.strip()}" for criterion in self.acceptance_criteria]
        if self.risks:
            lines += ["", "### Risks"]
            lines += [f"- {risk.strip()}" for risk in self.risks]
        return "\n".join(lines) + "\n"


class ImplementationSummary(_Answer):
    summary: Text = Field(description="What you changed, in two or three sentences.")
    files_changed: list[str] = Field(description="Every path you created or edited.")
    checks_run: list[str] = Field(
        description="The commands you ran to check your work and their result, e.g. "
        '"pytest: 12 passed"; [] if none.'
    )
    open_issues: list[str] = Field(
        description="Anything left undone, uncertain, or that a reviewer should know; [] if none."
    )


class TestSuiteSummary(_Answer):
    """The Tester's answer after writing or revising acceptance tests."""

    __test__ = False  # not a pytest test class
    summary: Text = Field(description="What the tests check, in two or three sentences.")
    tests: list[str] = Field(
        description="Every test you wrote or changed, as pytest node ids "
        '("tests/acceptance/test_x.py::test_y").'
    )
    criteria_covered: list[str] = Field(
        description="The acceptance criteria your tests check, quoted from the plan."
    )
    open_issues: list[str] = Field(description="Criteria you could not test, and why; [] if none.")


class TriageKind(StrEnum):
    IMPL_BUG = "impl_bug"
    TEST_BUG = "test_bug"
    ENVIRONMENT = "environment"


class TestTriage(_Answer):
    """The Tester's diagnosis of a consistent test failure (flakes are told apart by the
    harness, by rerunning, before any model is asked)."""

    __test__ = False  # not a pytest test class
    kind: TriageKind = Field(
        description=(
            "impl_bug: the implementation is wrong and the test is right; test_bug: the test "
            "is wrong; environment: neither can fix it (a missing tool or service)."
        )
    )
    test_id: Text = Field(description="The pytest node id of the failure you diagnosed.")
    diagnosis: Text = Field(
        description="What is wrong and where, specific enough for its owner to fix it."
    )


# ---------- review and negotiation (PLAN §4, ADR-014)


class FindingKind(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    PERFORMANCE = "performance"
    DESIGN = "design"
    STYLE = "style"
    PREFERENCE = "preference"

    @property
    def executable(self) -> bool:
        """Disputes a repro test can settle (ADR-004)."""
        return self in {FindingKind.CORRECTNESS, FindingKind.SECURITY}


class Location(_Answer):
    path: Text = Field(description="The file, relative to the repository root.")
    start_line: Annotated[int, Field(ge=1)] = Field(description="First line, 1-based.")
    end_line: Annotated[int, Field(ge=1)] = Field(
        description="Last line, inclusive; equal to start_line for one line."
    )

    @model_validator(mode="after")
    def _ordered(self) -> Location:
        if self.end_line < self.start_line:
            raise ValueError(f"end_line {self.end_line} precedes start_line {self.start_line}")
        return self

    def render(self) -> str:
        lines = (
            f"{self.start_line}"
            if self.start_line == self.end_line
            else f"{self.start_line}-{self.end_line}"
        )
        return f"{self.path}:{lines}"


class ReviewFinding(_Answer):
    """One finding, as the Reviewer writes it. The harness gives it an id, and decides
    whether it is admissible (it must point at lines the change touched)."""

    severity: Severity = Field(
        description=(
            "blocker: must not ship (wrong result, data loss, security hole); major: a real "
            "defect or a clear plan violation; minor: worth fixing, not blocking; nit: taste. "
            "Only blocker and major are negotiated; minor and nit are reported."
        )
    )
    kind: FindingKind
    location: Location = Field(
        description="Where the problem is, on lines this change added or modified."
    )
    claim: Text = Field(description="What is wrong, as a statement that is true or false.")
    evidence: Text = Field(
        description=(
            "Why you believe it: quoted code, an input that gives the wrong result, the plan "
            "criterion or preference it breaks."
        )
    )
    suggested_fix: str = Field(description='How to fix it, or "" if you have no fix to suggest.')
    preference_ref: str = Field(
        description='For kind "preference": the id of the preference it breaks; otherwise "".'
    )
    reopens: str = Field(
        description=(
            "The id of an earlier finding the author agreed to fix that is still not fixed; "
            'otherwise "". Only then may a finding point at lines the latest revision left '
            "unchanged."
        )
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="How likely you are right, from 0 to 1."
    )


class Review(_Answer):
    summary: Text = Field(description="Your overall assessment, in two or three sentences.")
    findings: list[ReviewFinding] = Field(description="Every problem you found; [] if none.")


class AuthorAction(StrEnum):
    ACCEPT = "accept"  # the finding is right: the code will change
    REBUT = "rebut"  # the finding is wrong, and here is the evidence
    COUNTER = "counter"  # the problem is real; a different fix is proposed
    DEFER = "defer"  # real but out of scope for this change: a follow-up


class AuthorMove(_Answer):
    finding_id: Text
    action: AuthorAction
    rationale: Text = Field(description="Why, for every action, accept included.")
    evidence: str = Field(
        description=(
            "For rebut: what shows the finding is wrong (quoted code, a test that passes, the "
            'plan text). For counter: the fix you propose instead. Otherwise "".'
        )
    )


class AuthorResponse(_Answer):
    moves: list[AuthorMove] = Field(description="One move per finding you were given.")


class ReviewerAction(StrEnum):
    WITHDRAW = "withdraw"  # the author is right: the finding is dropped (or deferred)
    MAINTAIN = "maintain"  # the finding stands as written
    AMEND = "amend"  # the finding stands, restated; for a counter: the counter is agreed


class ReviewerMove(_Answer):
    finding_id: Text
    action: ReviewerAction
    rationale: Text = Field(description="Why, judged on the author's evidence, not its tone.")
    amendment: str = Field(
        description='For amend: the finding as you now state it, or the fix you agree to; else "".'
    )


class ReviewerReply(_Answer):
    moves: list[ReviewerMove] = Field(description="One move per finding the author contested.")


class RulingVerdict(StrEnum):
    UPHELD = "upheld"  # the reviewer is right: the code must change
    DISMISSED = "dismissed"  # the author is right: no change
    DEFERRED = "deferred"  # real, but out of scope: a follow-up item


class FindingRuling(_Answer):
    finding_id: Text
    verdict: RulingVerdict
    rationale: Text = Field(description="The deciding reason, citing the evidence you weighed.")


class Ruling(_Answer):
    rulings: list[FindingRuling] = Field(description="One ruling per disputed finding.")


class ReproAttempt(_Answer):
    """The Tester's answer when asked to settle a correctness or security dispute."""

    written: bool = Field(
        description=(
            "Whether you wrote a test that checks the claim. false when the claim cannot be "
            "checked by a test (it is about design, or needs a service you do not have)."
        )
    )
    explanation: Text = Field(
        description="What the test checks and why it decides the claim, or why none can."
    )
