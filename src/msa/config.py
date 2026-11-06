"""``msa.toml``: the one file that holds every model id, beta flag, price, and limit.

When the API changes (a new model, a renamed beta, a price cut), the edit happens here and
nowhere else (PLAN §12). The schema is strict: unknown keys are errors, every role's model
must have a price, and money is computed in integer micro-dollars, rounded *up*, so the
ledger never under-reports spend.

The committed ``msa.toml`` is validated in CI, and ``schemas/msa.schema.json`` is generated
from this module (``msa config schema``) for editor completion.
"""

from __future__ import annotations

import json
import tomllib
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from msa.core._frozen import Frozen
from msa.core.values import Escalation, Limits, PlanGate

DEFAULT_CONFIG = Path("msa.toml")

# Bare model ids only (claude-opus-5-5). A dated snapshot suffix such as
# claude-opus-5-5-20260401 is a common copy-paste error, so version parts are 1-2 digits.
ModelId = Annotated[str, StringConstraints(pattern=r"^claude-[a-z]+(?:-\d{1,2})+$")]
BetaFlag = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+-\d{4}-\d{2}-\d{2}$")]
UsdPerMillionTokens = Annotated[Decimal, Field(ge=0, decimal_places=6)]


class ConfigError(Exception):
    pass


class Role(StrEnum):
    PLANNER = "planner"
    CODER = "coder"
    TESTER = "tester"
    REVIEWER = "reviewer"
    ARBITER = "arbiter"


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class RoleConfig(Frozen):
    model: ModelId | None = None  # None: use [models].default
    effort: Effort


class ModelsConfig(Frozen):
    default: ModelId
    roles: dict[Role, RoleConfig]

    @model_validator(mode="after")
    def _every_role_configured(self) -> Self:
        missing = sorted(role.value for role in set(Role) - set(self.roles))
        if missing:
            raise ValueError(f"[models.roles] is missing {missing}")
        return self

    def model_for(self, role: Role) -> str:
        return self.roles[role].model or self.default

    def effort_for(self, role: Role) -> Effort:
        return self.roles[role].effort


class BetasConfig(Frozen):
    server_side_fallback: BetaFlag
    task_budgets: BetaFlag
    thinking_binding: BetaFlag


class ApiConfig(Frozen):
    # Always explicit: the SDK would otherwise read ANTHROPIC_BASE_URL from the environment,
    # which some hosts (e.g. coding-agent containers) set for their own traffic.
    base_url: Annotated[str, StringConstraints(pattern=r"^https://[a-z0-9.-]+(:\d+)?/?$")]
    max_tokens: PositiveInt
    min_output_tokens: PositiveInt = 1024  # below this, a turn is not worth starting
    # Retries per request: the SDK's (408/409/429/5xx, connection errors) before a response
    # starts, and the gateway's for failures after the stream started, which the SDK cannot
    # retry. The gateway waits retry_backoff_seconds, doubling each time.
    max_retries: NonNegativeInt = 2
    retry_backoff_seconds: PositiveFloat = 2.0
    timeout_seconds: PositiveFloat = 600.0
    fallbacks: str  # "default": route refusals by category on the server
    betas: BetasConfig


class Pricing(Frozen):
    """US dollars per million tokens, as published."""

    input: UsdPerMillionTokens
    output: UsdPerMillionTokens
    cache_read: UsdPerMillionTokens
    cache_write_5m: UsdPerMillionTokens
    cache_write_1h: UsdPerMillionTokens

    def cost_usd_micros(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
    ) -> int:
        """Exact cost in micro-dollars, rounded up. ($X per MTok is X micro-dollars per token.)"""
        total = (
            self.input * input_tokens
            + self.output * output_tokens
            + self.cache_read * cache_read_tokens
            + self.cache_write_5m * cache_write_5m_tokens
            + self.cache_write_1h * cache_write_1h_tokens
        )
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    @property
    def worst_input_rate(self) -> Decimal:
        """The most an input token can cost: uncached, or written to either cache."""
        return max(self.input, self.cache_write_5m, self.cache_write_1h)


class LimitsConfig(Frozen):
    """Defaults for every run. The loop bounds default to the core's own defaults, which are
    the limits the state machine is exhaustively model-checked at."""

    budget_usd: PositiveFloat = 5.0
    wall_clock_minutes: PositiveInt = 45
    max_replans: NonNegativeInt = Limits.model_fields["max_replans"].default
    max_revisions: NonNegativeInt = Limits.model_fields["max_revisions"].default
    max_negotiation_rounds: PositiveInt = Limits.model_fields["max_negotiation_rounds"].default
    plan_gate: PlanGate = PlanGate.AUTO
    escalation: Escalation = Escalation.HUMAN

    def to_limits(self, **overrides: Any) -> Limits:
        values: dict[str, Any] = {
            "budget_usd_micros": round(self.budget_usd * 1_000_000),
            "wall_clock": timedelta(minutes=self.wall_clock_minutes),
            "max_replans": self.max_replans,
            "max_revisions": self.max_revisions,
            "max_negotiation_rounds": self.max_negotiation_rounds,
            "plan_gate": self.plan_gate,
            "escalation": self.escalation,
        }
        return Limits.model_validate({**values, **overrides})


class JournalConfig(Frozen):
    path: Path = Path(".msa/runs.db")
    snapshot_every: PositiveInt = 50


class MsaConfig(Frozen):
    models: ModelsConfig
    api: ApiConfig
    pricing: dict[ModelId, Pricing]
    limits: LimitsConfig = LimitsConfig()
    journal: JournalConfig = JournalConfig()

    @model_validator(mode="after")
    def _every_model_is_priced(self) -> Self:
        used = {self.models.model_for(role) for role in Role}
        unpriced = sorted(used - set(self.pricing))
        if unpriced:
            raise ValueError(f"no [pricing] entry for {unpriced}: spend could not be accounted")
        return self

    def pricing_for(self, role: Role) -> Pricing:
        return self.pricing[self.models.model_for(role)]

    def pricing_for_model(self, model: str | None) -> Pricing:
        """Exact pricing for a served model, or a ceiling for one the table does not list.

        A refusal fallback can be served by a model the table lacks. Pricing every
        component at the highest configured rate over-states such spend; it never
        under-states it, which is the direction the budget invariant needs.
        """
        if model is not None and model in self.pricing:
            return self.pricing[model]
        rates = list(self.pricing.values())
        return Pricing(
            input=max(r.input for r in rates),
            output=max(r.output for r in rates),
            cache_read=max(r.cache_read for r in rates),
            cache_write_5m=max(r.cache_write_5m for r in rates),
            cache_write_1h=max(r.cache_write_1h for r in rates),
        )


def load_config(path: Path = DEFAULT_CONFIG) -> MsaConfig:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    try:
        return MsaConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise ConfigError(f"{path}: {problems}") from exc


def json_schema() -> str:
    schema = MsaConfig.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "msa.toml"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"
