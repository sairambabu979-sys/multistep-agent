""".github/workflows/eval.yml: the properties of a workflow that spends, held as tests."""

from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML

from tests.support.scripts import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "eval.yml"


def workflow() -> dict[str, Any]:
    loaded: dict[str, Any] = YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step.get("name", step.get("uses", "")): step for step in job["steps"]}


def test_it_runs_only_when_a_person_dispatches_it_with_both_caps() -> None:
    on = workflow()["on"]
    assert list(on) == ["workflow_dispatch"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["max_total_usd"]["required"]
    assert inputs["budget_usd"]["required"]
    run = steps(workflow()["jobs"]["eval"])["The eval (real model)"]["run"]
    assert '--max-total-usd "$MAX_TOTAL_USD"' in run
    assert '--budget-usd "$BUDGET_USD"' in run


def test_only_the_spending_step_holds_the_model_key() -> None:
    job = workflow()["jobs"]["eval"]
    holding = [name for name, step in steps(job).items() if "secrets." in str(step)]
    assert holding == ["The eval (real model)"]
    assert "secrets." not in str(job.get("env", {}))


def test_every_task_is_checked_before_anything_spends() -> None:
    names = list(steps(workflow()["jobs"]["eval"]))
    assert names.index("Every task is fit to score (spends nothing)") < names.index(
        "The eval (real model)"
    )


def test_it_waits_its_turn_with_the_live_runs() -> None:
    job = workflow()["jobs"]["eval"]
    assert job["concurrency"] == {"group": "live-run-spend", "cancel-in-progress": False}


def test_inputs_reach_the_scripts_as_environment_variables_only() -> None:
    job = workflow()["jobs"]["eval"]
    for step in job["steps"]:
        assert "inputs." not in str(step.get("run", "")), step.get("name")
    assert job["env"]["ARMS"] == "${{ inputs.arms }}"


def test_agent_commands_and_hidden_tests_run_without_network() -> None:
    names = list(steps(workflow()["jobs"]["eval"]))
    assert "Network namespaces (agent commands and hidden tests get no network)" in names
    settings = steps(workflow()["jobs"]["eval"])["Settings for this eval"]["run"]
    assert "scripts/live_run_settings.sh" in settings  # isolate_network = "always"
