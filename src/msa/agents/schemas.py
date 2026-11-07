"""The typed answers agents return (PLAN §3), validated through structured outputs.

Every model is strict-compatible (``msa.llm.schema``): closed, every field required, and
no constraint strict mode rejects. Pydantic still enforces the constraints client-side, so
an answer that breaks one fails the invocation instead of reaching the state machine.

The state machine never sees these. It sees a ``PlanRef`` (an id and the digest of the
stored plan), and the executors keep the full answers as run artifacts.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

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
