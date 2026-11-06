"""What a response cost, and how much the next turn may spend.

**Pricing a response.** When a refusal fallback ran, ``usage.iterations`` holds one entry
per attempt, each at its own model's rates, and top-level ``usage`` covers only the final
attempt. Pricing from top-level usage alone would under-report, so attempts are summed.

**Planning a turn.** The effect's reservation is a hard cap. Output cost is bounded exactly
by ``max_tokens`` (thinking counts as output). Input cost is bounded by pricing every input
token at its worst rate (uncached, or written to either cache). The input token count is a
conservative estimate unless that estimate makes the budget bind; then it is counted
exactly, which is free and only spent on the turns where precision matters.
"""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from anthropic.types.beta import BetaMessage

from msa.config import MsaConfig, Pricing
from msa.core.budget import Spend

# JSON is denser than prose, but rarely below ~2.5 characters per token; 2 is conservative.
CHARS_PER_TOKEN_FLOOR = 2


def spend_of(message: BetaMessage, config: MsaConfig) -> Spend:
    usage = message.usage
    attempts: list[Any] = list(usage.iterations) if usage.iterations else [usage]
    total = Spend()
    for attempt in attempts:
        model = getattr(attempt, "model", None) or message.model
        pricing = config.pricing_for_model(model)
        creation = attempt.cache_creation
        write_1h = creation.ephemeral_1h_input_tokens if creation else 0
        write_5m = (
            creation.ephemeral_5m_input_tokens if creation else attempt.cache_creation_input_tokens
        ) or 0
        read = attempt.cache_read_input_tokens or 0
        total += Spend(
            usd_micros=pricing.cost_usd_micros(
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                cache_read_tokens=read,
                cache_write_5m_tokens=write_5m,
                cache_write_1h_tokens=write_1h,
            ),
            input_tokens=attempt.input_tokens + read + write_5m + write_1h,
            output_tokens=attempt.output_tokens,
        )
    return total


def estimate_input_tokens(request: dict[str, Any]) -> int:
    """A deliberately high estimate of a request's input tokens."""
    return len(json.dumps(request, ensure_ascii=False)) // CHARS_PER_TOKEN_FLOOR + 1


def affordable_output_tokens(
    *, remaining_usd_micros: int, input_tokens: int, pricing: Pricing, cap: int
) -> int:
    """The largest ``max_tokens`` whose worst case fits the remaining budget (at most ``cap``)."""
    input_cost = (pricing.worst_input_rate * input_tokens).to_integral_value(ROUND_CEILING)
    left = Decimal(remaining_usd_micros) - input_cost
    if left <= 0:
        return 0
    if pricing.output == 0:
        return cap
    return min(cap, int((left / pricing.output).to_integral_value(ROUND_FLOOR)))


def task_budget_tokens(budget_usd_micros: int, pricing: Pricing) -> int:
    """The output-token equivalent of a whole invocation's reservation."""
    if pricing.output <= 0:
        return 0
    return int((Decimal(budget_usd_micros) / pricing.output).to_integral_value(ROUND_FLOOR))
