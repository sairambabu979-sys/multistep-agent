"""Pricing responses exactly and planning turns that cannot overspend.

Rates from msa.toml, in micro-USD per token: claude-opus-5-5 is 4 in / 20 out / 0.2 cache
read / 5 and 8 cache write; claude-opus-5 is 5 / 25 / 0.5 / 6.25 / 10.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage
from hypothesis import given
from hypothesis import strategies as st

from msa.config import Pricing
from msa.core.budget import Spend
from msa.llm import fake
from msa.llm.accounting import (
    affordable_output_tokens,
    estimate_input_tokens,
    spend_of,
    task_budget_tokens,
)
from tests.support.llm import config

REQUESTED = "claude-opus-5-5"


def priced(usage: dict[str, Any], model: str = REQUESTED) -> Spend:
    return spend_of(BetaMessage.model_validate(fake.message(model=model, usage_=usage)), config())


def pricing(**rates: str) -> Pricing:
    values = {"input": "1", "output": "1", "cache_read": "0", "cache_write_5m": "1"}
    return Pricing.model_validate({**values, "cache_write_1h": "1", **rates})


# ---------- spend_of


def test_a_plain_turn_is_priced_at_the_served_models_rates() -> None:
    assert priced(fake.usage(100, 50)) == Spend(
        usd_micros=100 * 4 + 50 * 20, input_tokens=100, output_tokens=50
    )


def test_cache_reads_and_writes_are_priced_and_counted_as_input() -> None:
    spend = priced(fake.usage(100, 50, cache_read=1_000, cache_write=200))
    assert spend.usd_micros == 100 * 4 + 50 * 20 + 1_000 * 0.2 + 200 * 5
    assert spend.input_tokens == 100 + 1_000 + 200


def test_one_hour_cache_writes_are_priced_from_the_breakdown() -> None:
    usage = fake.usage(100, 50, cache_write=300)
    usage["cache_creation"] = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}
    spend = priced(usage)
    assert spend.usd_micros == 100 * 4 + 50 * 20 + 100 * 5 + 200 * 8
    assert spend.input_tokens == 100 + 300


def test_without_a_breakdown_cache_writes_are_charged_at_the_five_minute_rate() -> None:
    # The gateway only writes 5-minute entries, so that is what an unlabelled write is.
    assert priced(fake.usage(0, 0, cache_write=10)).usd_micros == 10 * 5


def test_a_fallback_bills_every_attempt_at_its_own_models_rates() -> None:
    usage = fake.usage(
        100,
        50,  # top-level usage is the serving attempt only: summing it too would double count
        iterations=[
            fake.attempt(REQUESTED, 100, 10),
            fake.attempt("claude-opus-5", 100, 50, fallback=True),
        ],
    )
    spend = priced(usage, model="claude-opus-5")
    assert spend == Spend(
        usd_micros=(100 * 4 + 10 * 20) + (100 * 5 + 50 * 25), input_tokens=200, output_tokens=60
    )


def test_an_attempt_by_a_model_missing_from_the_table_is_priced_at_the_ceiling() -> None:
    usage = fake.usage(iterations=[fake.attempt("claude-unlisted-9", 100, 50, fallback=True)])
    # The highest configured rate of each component: 5 in, 25 out.
    assert priced(usage, model="claude-unlisted-9").usd_micros == 100 * 5 + 50 * 25


def test_an_iteration_without_a_model_is_priced_as_the_message() -> None:
    compaction = {
        "type": "compaction",
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    usage = fake.usage(iterations=[compaction, fake.attempt(REQUESTED, 10, 5)])
    assert priced(usage).usd_micros == (1_000 * 4 + 100 * 20) + (10 * 4 + 5 * 20)


# ---------- estimate_input_tokens


def test_the_estimate_counts_characters_not_escapes() -> None:
    assert estimate_input_tokens({"a": "xxxx"}) == len('{"a": "xxxx"}') // 2 + 1
    assert estimate_input_tokens({"a": "é"}) == len('{"a": "é"}') // 2 + 1


@given(st.text(max_size=400))
def test_the_estimate_never_falls_below_half_a_token_per_character(value: str) -> None:
    request = {"messages": [{"role": "user", "content": value}]}
    assert estimate_input_tokens(request) * 2 > len(json.dumps(request, ensure_ascii=False))


# ---------- affordable_output_tokens


def plan(remaining: int, input_tokens: int, rates: Pricing | None = None, cap: int = 64_000) -> int:
    return affordable_output_tokens(
        remaining_usd_micros=remaining,
        input_tokens=input_tokens,
        pricing=rates or config().pricing_for_model(REQUESTED),
        cap=cap,
    )


def test_input_is_reserved_at_its_worst_rate_and_the_rest_buys_output() -> None:
    # 10k input tokens at the 1-hour write rate (8) reserve 80k; 920k buys 46k output at 20.
    assert plan(1_000_000, 10_000) == 46_000


def test_the_cap_binds_when_the_budget_is_ample() -> None:
    assert plan(10_000_000, 10_000) == 64_000
    assert plan(10_000_000, 10_000, cap=1) == 1


@pytest.mark.parametrize("remaining", [80_000, 79_999, 0, -5])
def test_nothing_is_affordable_once_the_input_alone_uses_the_budget(remaining: int) -> None:
    assert plan(remaining, 10_000) == 0


def test_output_tokens_round_down_and_input_cost_rounds_up() -> None:
    assert plan(80_039, 10_000) == 1  # 39 left buys one token at 20, not two
    # One input token at 0.3 costs 1 micro-USD, not 0.3: 1 left buys 2 tokens at 0.5, not 3.
    rates = pricing(input="0.3", output="0.5", cache_write_5m="0.3", cache_write_1h="0.3")
    assert plan(2, 1, rates) == 2


def test_free_output_is_capped() -> None:
    assert plan(1, 0, pricing(output="0"), cap=777) == 777


@given(
    remaining=st.integers(min_value=0, max_value=10**9),
    input_tokens=st.integers(min_value=0, max_value=10**6),
    cap=st.integers(min_value=1, max_value=128_000),
)
def test_the_worst_case_of_the_plan_always_fits_the_budget(
    remaining: int, input_tokens: int, cap: int
) -> None:
    rates = config().pricing_for_model(REQUESTED)
    tokens = plan(remaining, input_tokens, cap=cap)
    assert 0 <= tokens <= cap
    if tokens:
        worst = rates.worst_input_rate * input_tokens + rates.output * tokens
        assert worst <= remaining


# ---------- task_budget_tokens


def test_the_task_budget_is_the_reservation_in_output_tokens() -> None:
    rates = config().pricing_for_model(REQUESTED)
    assert task_budget_tokens(2_000_000, rates) == 100_000
    assert task_budget_tokens(2_000_019, rates) == 100_000
    assert task_budget_tokens(2_000_000, pricing(output="0")) == 0


def test_worst_input_rate_is_the_highest_way_to_pay_for_an_input_token() -> None:
    assert pricing(input="3", cache_write_5m="2", cache_write_1h="1").worst_input_rate == 3
    assert pricing(input="1", cache_write_5m="2", cache_write_1h="1").worst_input_rate == 2
    assert config().pricing_for_model(REQUESTED).worst_input_rate == Decimal("8")
