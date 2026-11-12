from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from msa.config import (
    ConfigError,
    Effort,
    LimitsConfig,
    Pricing,
    Role,
    json_schema,
    load_config,
)
from msa.core.values import Escalation, Limits, PlanGate

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "msa.toml"


def write(tmp_path: Path, data: str) -> Path:
    path = tmp_path / "msa.toml"
    path.write_text(data, encoding="utf-8")
    return path


def test_the_committed_config_is_valid_and_complete() -> None:
    config = load_config(COMMITTED)
    assert {config.models.model_for(role) for role in Role} == {"claude-opus-5-5"}
    assert config.models.effort_for(Role.CODER) is Effort.XHIGH
    assert config.pricing_for(Role.REVIEWER).output == Decimal("20.00")


def test_the_committed_schema_matches_the_model() -> None:
    schema = ROOT / "schemas" / "msa.schema.json"
    assert schema.read_text(encoding="utf-8") == json_schema(), (
        "schemas/msa.schema.json is stale. Regenerate it with: "
        "uv run msa config schema --output schemas/msa.schema.json"
    )


def test_no_model_id_is_hard_coded_in_the_source() -> None:
    """PLAN §12: model ids live in msa.toml and nowhere else."""
    pattern = re.compile(r"claude-[a-z]+-\d")
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}: {node.value!r}"
        for path in (ROOT / "src").rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and pattern.search(node.value)
        and "\n" not in node.value  # docstrings may mention a model by name
    ]
    assert offenders == []


# ---------- pricing


def test_cost_is_exact_in_micro_dollars() -> None:
    price = load_config(COMMITTED).pricing_for(Role.CODER)
    # $4 per million input tokens is 4 micro-dollars per token.
    assert price.cost_usd_micros(input_tokens=1_000_000) == 4_000_000
    assert price.cost_usd_micros(input_tokens=1_000, output_tokens=500) == 4_000 + 10_000
    assert price.cost_usd_micros(
        cache_read_tokens=10, cache_write_5m_tokens=10, cache_write_1h_tokens=10
    ) == round(10 * 0.20 + 10 * 5 + 10 * 8)


def test_fractional_costs_round_up_so_the_ledger_never_under_reports() -> None:
    price = load_config(COMMITTED).pricing_for(Role.CODER)
    assert price.cost_usd_micros(cache_read_tokens=1) == 1  # 0.2 micro-dollars
    assert price.cost_usd_micros() == 0


def test_prices_cannot_be_negative_or_over_precise() -> None:
    fields = {"output": 1, "cache_read": 1, "cache_write_5m": 1, "cache_write_1h": 1}
    with pytest.raises(ValueError, match="greater than or equal"):
        Pricing.model_validate({"input": -1, **fields})
    with pytest.raises(ValueError, match="decimal places"):
        Pricing.model_validate({"input": "0.0000001", **fields})


# ---------- limits


def test_limit_defaults_are_the_model_checked_core_defaults() -> None:
    defaults = LimitsConfig().to_limits()
    core = Limits(budget_usd_micros=1, wall_clock=defaults.wall_clock)
    assert (defaults.max_replans, defaults.max_revisions, defaults.max_negotiation_rounds) == (
        core.max_replans,
        core.max_revisions,
        core.max_negotiation_rounds,
    )
    assert defaults.budget_usd_micros == 5_000_000


def test_limits_accept_overrides() -> None:
    limits = LimitsConfig().to_limits(plan_gate=PlanGate.HUMAN, budget_usd_micros=7)
    assert (limits.plan_gate, limits.budget_usd_micros) == (PlanGate.HUMAN, 7)
    assert limits.escalation is Escalation.HUMAN


# ---------- validation: mistakes are caught at load time, with their location


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'default = "claude-opus-5-5"',
            'default = "claude-opus-5-5-20260401"',  # a dated snapshot id
            "models.default",
        ),
        ('arbiter = { effort = "high" }\n', "", "missing ['arbiter']"),
        ('coder = { effort = "xhigh" }', 'coder = { effort = "extreme" }', "effort"),
        (
            'coder = { effort = "xhigh" }',
            'coder = { model = "claude-sonnet-5-5", effort = "xhigh" }',
            "no [pricing] entry for ['claude-sonnet-5-5']",
        ),
        ('task_budgets = "task-budgets-2026-03-13"', 'task_budgets = "task-budgets"', "betas"),
        ("max_tokens = 64000", "max_tokens = 64000\nsurprise = 1", "api.surprise"),
        ("max_negotiation_rounds = 3", "max_negotiation_rounds = 0", "max_negotiation_rounds"),
    ],
)
def test_invalid_settings_are_rejected_with_their_location(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    text = COMMITTED.read_text(encoding="utf-8")
    assert old in text
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write(tmp_path, text.replace(old, new, 1)))


def test_missing_and_malformed_files_are_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[models\n"))
