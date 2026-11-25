"""msa.evals.report: the arms' metrics, the paired test, and the registered decision rule."""

from __future__ import annotations

import pytest

from msa.evals import report
from msa.evals.runner import FindingRow, Result
from tests.support.scripts import ROOT

RULE = report.Rule(
    name="review",
    with_arm="D",
    without_arm="B",
    min_resolve_gain=0.10,
    max_p=0.05,
    max_cost_ratio=2.0,
    min_runs=3,
)


def result(
    task: str, arm: str, resolved: bool | None, spend: float = 1.0, **extra: object
) -> Result:
    trap = resolved is None
    return Result.model_validate(
        {
            "task": task,
            "kind": "trap" if trap else "bugfix",
            "expect": "flag" if trap else "resolve",
            "arm": arm,
            "repeat": 0,
            "run_id": f"eval-{task}-{arm}-0",
            "phase": "human_approval",
            "outcome": None,
            "delivered": True,
            "resolved": resolved,
            "spend_usd": spend,
            **extra,
        }
    )


def finding(fid: str, verdict: str | None, severity: str = "blocker") -> FindingRow:
    return FindingRow(
        id=fid,
        severity=severity,
        kind="correctness",
        admitted=True,
        claim=f"claim {fid}",
        verdict=verdict,
        rung="negotiation",
    )


@pytest.mark.parametrize(
    ("only_with", "only_without", "p"),
    [
        (0, 0, 1.0),
        (2, 0, 0.25),
        (5, 0, 1 / 32),
        (6, 0, 1 / 64),
        (0, 6, 1.0),  # one-sided: the component losing every disagreement is no evidence for it
        (3, 3, 42 / 64),
        (4, 1, 6 / 32),
        (12, 4, 2517 / 65536),
    ],
)
def test_the_one_sided_exact_mcnemar_p_value(only_with: int, only_without: int, p: float) -> None:
    assert report.exact_mcnemar(only_with, only_without) == pytest.approx(p)


def test_an_arms_metrics() -> None:
    results = [
        result(
            "a",
            "D",
            True,
            2.0,
            findings=[finding("c0-1", "accepted"), finding("c0-2", "withdrawn")],
        ),
        result(
            "b",
            "D",
            False,
            1.0,
            findings=[finding("c0-1", "dismissed"), finding("c0-2", None, "nit")],
        ),
        result("c", "D", True, 1.0, cache_read_tokens=300, input_tokens=100),
        result("t", "D", None, 0.5),  # a trap: spent, but not a task to resolve
        result("e", "D", False, 0.0, error="ValueError: the harness broke"),
    ]
    (d,) = report.summarize(results).values()
    assert (d.runs, d.errors, d.to_resolve, d.resolved) == (5, 1, 3, 2)
    assert d.resolve_rate == pytest.approx(2 / 3)
    assert d.cost_per_resolved == pytest.approx(4.0 / 2)  # the trap's spend is not counted
    assert d.spend == pytest.approx(4.5)
    assert d.blocking == 3  # the nit is not blocking
    assert d.false_blocker_rate == pytest.approx(2 / 3)  # withdrawn + dismissed of 3 settled
    assert d.acceptance_rate == pytest.approx(1 / 3)
    assert d.withdrawal_rate == pytest.approx(1 / 2)  # of the 2 the author contested
    assert d.degenerate == []
    assert d.cache_hit_rate == pytest.approx(300 / 400)


def test_a_degenerate_negotiation_is_flagged() -> None:
    everything_accepted = [finding(f"c0-{i}", "accepted") for i in range(20)]
    (d,) = report.summarize([result("a", "D", True, findings=everything_accepted)]).values()
    assert d.degenerate == ["the author accepted over 95% of blocking findings"]


def _arms(
    with_resolved: list[bool], without_resolved: list[bool], with_spend: float = 1.0
) -> list[Result]:
    return [
        *(result(f"t{i}", "D", r, with_spend) for i, r in enumerate(with_resolved)),
        *(result(f"t{i}", "B", r, 1.0) for i, r in enumerate(without_resolved)),
    ]


def test_too_few_runs_decide_nothing() -> None:
    results = _arms([True, True], [False, False])
    d = report.decide(RULE, report.summarize(results), results)
    assert d.verdict == "not enough evidence"


def test_a_component_that_clears_every_margin_stays_enabled() -> None:
    results = _arms([True] * 6, [True, False, False, False, False, False], with_spend=1.5)
    d = report.decide(RULE, report.summarize(results), results)
    assert d.verdict == "keep it enabled"
    assert d.gain == pytest.approx(5 / 6)
    assert d.p_value == pytest.approx(1 / 32)
    assert d.cost_ratio == pytest.approx((9.0 / 6) / (6.0 / 1))
    assert d.discordant == (5, 0, 6)


def test_a_gain_that_could_be_chance_ships_it_disabled() -> None:
    # 50 points of resolve rate, cheaply, but on two disagreements: p = 0.25.
    results = _arms([True, True, True, False], [True, False, False, False])
    d = report.decide(RULE, report.summarize(results), results)
    assert d.verdict == "ship it disabled"
    assert d.gain == pytest.approx(0.5)
    assert d.reasons == ["the paired test gives p = 0.250, over 0.05: it could be chance"]


def test_too_small_a_gain_ships_it_disabled() -> None:
    results = _arms([True, False, False, False], [True, False, False, False])
    d = report.decide(RULE, report.summarize(results), results)
    assert d.verdict == "ship it disabled"
    assert "under the 10% margin" in d.reasons[0]


def test_too_dear_a_gain_ships_it_disabled() -> None:
    results = _arms([True, True, True, True], [True, True, True, False], with_spend=10.0)
    d = report.decide(RULE, report.summarize(results), results)
    assert d.verdict == "ship it disabled"
    assert any("over the 2.00x cap" in reason for reason in d.reasons)


def test_the_registered_rule_loads() -> None:
    (rule,) = report.load_rules(ROOT / "evals" / "decision.toml")
    assert (rule.name, rule.with_arm, rule.without_arm) == ("review", "D", "B")
    assert (rule.min_resolve_gain, rule.max_p, rule.max_cost_ratio, rule.min_runs) == (
        0.10,
        0.05,
        2.0,
        54,
    )


def test_the_report_shows_the_decision_and_what_a_person_must_judge() -> None:
    results = [
        *_arms([True, True, True, False], [True, False, False, False]),
        result("trap", "D", None, plan_risks=["'properly' is undefined"]),
        result("x", "D", True, findings=[finding("c0-1", "upheld")]),
    ]
    text = report.render(results, [RULE], rule_commit="abc1234 2026-11-24")
    assert "# Eval report" in text
    assert "**review** (arm D against B): **ship it disabled**." in text
    assert "- the paired test gives p = 0.250, over 0.05: it could be chance." in text
    assert "exact one-sided McNemar p = 0.250 (could be chance)." in text
    assert "last changed in `abc1234 2026-11-24`" in text
    assert "- `trap`, arm D, delivered: 'properly' is undefined" in text
    assert "- `x` D `c0-1` [blocker, upheld]: claim c0-1" in text
    assert "| t3 | bugfix | ✗ $1.00 | ✗ $1.00 |" in text
