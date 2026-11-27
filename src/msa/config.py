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
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from msa import globs
from msa.core._frozen import Frozen
from msa.core.values import Escalation, Limits, PlanGate, PublishMode

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
    context_management: BetaFlag = "context-management-2025-06-27"


class ContextManagementConfig(Frozen):
    """The server clears old tool results from what the model sees (ADR-022).

    The transcript the gateway sends is untouched, so the journal, replay and thinking
    blocks are unaffected. Clearing invalidates the cached conversation from the first
    result it clears, so the next request writes what follows again, at the write price.
    It pays only when a lot is cleared, which is why the thresholds are high: this keeps a
    long Coder or Tester invocation from growing without bound, more than it saves money."""

    enabled: bool = True
    # Clear once the prompt reaches this many input tokens...
    trigger_input_tokens: PositiveInt = 200_000
    # ...keeping the most recent tool uses whole...
    keep_tool_uses: PositiveInt = 8
    # ...and only when at least this much would go: a smaller clear costs more than it saves.
    clear_at_least_input_tokens: PositiveInt = 60_000


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
    # How long the prompt cache keeps the system prompt and the conversation (ADR-021).
    # "1h" outlives a test suite or a human gate between turns, at 2x input per token
    # written instead of 1.25x; reads cost the same. Both breakpoints use it, so the
    # API's rule that a longer TTL comes first always holds.
    cache_ttl: Literal["5m", "1h"] = "1h"
    context_management: ContextManagementConfig = ContextManagementConfig()


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


POSTGRES_SCHEMES = ("postgresql", "postgres")


def postgres_url_problem(url: str) -> str | None:
    """Why ``url`` may not name a PostgreSQL journal, or None. A password in it is refused:
    it would end up in settings files, logs and shell history. libpq reads it from
    ``PGPASSWORD`` or ``~/.pgpass`` instead."""
    parts = urlsplit(url)
    if parts.scheme not in POSTGRES_SCHEMES:
        return f"a PostgreSQL journal URL starts with postgresql://, not {parts.scheme}://"
    if parts.password is not None:
        return (
            "the journal URL holds a password; leave it out and set PGPASSWORD or use "
            "~/.pgpass instead"
        )
    return None


def _postgres_url(url: str | None) -> str | None:
    if url is not None and (problem := postgres_url_problem(url)):
        raise ValueError(problem)
    return url


class JournalConfig(Frozen):
    # The SQLite journal. Its directory is the state directory either way: run artifacts,
    # tokens and the audit log live there.
    path: Path = Path(".msa/runs.db")
    # A postgresql:// URL, without a password: the journal is kept in PostgreSQL instead,
    # in its own schema, so one database can hold several deployments' journals.
    url: Annotated[str | None, AfterValidator(_postgres_url)] = None
    schema_name: Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,62}$")] = "msa"
    snapshot_every: PositiveInt = 50


# ---------- capabilities (ADR-006) and the sandbox (ADR-009)


Glob = Annotated[str, AfterValidator(globs.validate)]  # syntax: msa.globs

# Environment variables a sandboxed command may inherit. Names that look like credentials
# are refused here, so no msa.toml edit can hand a key to repository code.
_SECRET_LIKE = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE")


def _env_name(name: str) -> str:
    if any(marker in name.upper() for marker in _SECRET_LIKE):
        raise ValueError(f"{name!r} looks like a credential and is never passed to commands")
    return name


EnvName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"), AfterValidator(_env_name)
]
# A variable the harness itself reads, such as a token's: never passed to a command, so a
# name that looks like a credential is expected here.
HarnessVariable = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class RoleCapability(Frozen):
    """What one role may write: paths matching ``write`` and not ``deny``."""

    write: tuple[Glob, ...]
    deny: tuple[Glob, ...] = ()


_TESTER_PATHS = ("tests/acceptance/**", "tests/repro/**")


class CapabilitiesConfig(Frozen):
    # No role may write these, whatever its own globs say. `**/.git` matters in a linked
    # worktree (and a submodule), where .git is a file naming the git directory.
    protected: tuple[Glob, ...] = ("**/.git", "**/.git/**", ".msa", ".msa/**")
    roles: dict[Role, RoleCapability] = Field(
        default_factory=lambda: {
            Role.PLANNER: RoleCapability(write=()),
            Role.CODER: RoleCapability(write=("**",), deny=_TESTER_PATHS),
            Role.TESTER: RoleCapability(write=_TESTER_PATHS),
            Role.REVIEWER: RoleCapability(write=()),
            Role.ARBITER: RoleCapability(write=()),
        }
    )

    @model_validator(mode="after")
    def _every_role_configured(self) -> Self:
        missing = sorted(role.value for role in set(Role) - set(self.roles))
        if missing:
            raise ValueError(f"[capabilities.roles] is missing {missing}")
        return self

    def owned_directories(self, role: Role) -> tuple[str, ...]:
        """The directories a role owns outright: its ``<dir>/**`` write globs. VERIFYING
        leaves these to their owner's own checks (the Tester's tests run in TESTING)."""
        return tuple(
            glob.removesuffix("/**")
            for glob in self.roles[role].write
            if glob.endswith("/**") and glob != "**" and not any(c in glob[:-3] for c in "*?[")
        )


class ContainerConfig(Frozen):
    """The container backend for repository commands (ADR-019): the ``run`` tool and the
    test gates run in a fresh container per command, with the worktree mounted and
    nothing else writable, no network, a non-root user, and CPU, memory and process
    limits. ``image`` must hold the repository's toolchain (pytest, ruff, mypy, and its
    dependencies): nothing is installed in the container, which has no network to do it."""

    runtime: Literal["docker", "podman"] = "docker"
    image: str = ""  # required with backend = "container", e.g. "ghcr.io/me/repo-ci:1"
    # The image's interpreter, in place of [sandbox].launcher: `<launcher> pytest ...`.
    launcher: Annotated[tuple[str, ...], Field(min_length=1)] = ("python", "-m")
    cpus: PositiveFloat = 2.0
    memory: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*[kmg]$")] = "4g"
    pids_limit: PositiveInt = 512
    tmp_size: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*[kmg]$")] = "512m"
    # uid:gid inside the container; empty: yours, or 65534:65534 (nobody) if you are root.
    user: Annotated[str, StringConstraints(pattern=r"^([1-9][0-9]*:[0-9]+)?$")] = ""


class SandboxConfig(Frozen):
    """How commands run in the worktree sandbox (PLAN §8, ADR-009, ADR-019)."""

    # local: on this machine, as you (trusted repositories only). container: repository
    # commands in a container ([sandbox.container]); fails closed if there is no runtime.
    backend: Literal["local", "container"] = "local"
    container: ContainerConfig = ContainerConfig()

    # Prefix for allowlisted Python tools: `<launcher> pytest ...`.
    launcher: Annotated[tuple[str, ...], Field(min_length=1)] = (
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
    )
    timeout_seconds: PositiveFloat = 600.0
    output_limit_bytes: PositiveInt = 64 * 1024  # head and tail are kept, the middle elided
    read_limit_bytes: PositiveInt = 2 * 1024 * 1024  # larger files cannot be read
    # The most characters one read_file returns. A larger range is cut at a line boundary,
    # and the view says which start_line reads on (ADR-022). Every view stays in the
    # transcript for the rest of the invocation, so this bounds what each read adds.
    view_limit_chars: PositiveInt = 32_000
    search_limit_matches: PositiveInt = 200
    cpu_seconds: PositiveInt = 1800  # RLIMIT_CPU per command
    max_file_bytes: PositiveInt = 1024**3  # RLIMIT_FSIZE per command
    env_passthrough: tuple[EnvName, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM")
    # Commands an agent runs (its `run` tool) get a network namespace of their own
    # (`unshare -rn`): no host is reachable, whatever the program does with proxies.
    # auto: where the kernel allows it; always: refuse to run them where it does not;
    # never: only the dead proxy in their environment.
    isolate_network: Literal["auto", "always", "never"] = "auto"

    @model_validator(mode="after")
    def _a_container_needs_an_image(self) -> Self:
        if self.backend == "container" and not self.container.image:
            raise ValueError(
                '[sandbox].backend = "container" needs [sandbox.container].image: an image '
                "holding the repository's toolchain and dependencies"
            )
        return self


# The sandbox's allowlisted commands (msa.tools.sandbox.COMMANDS; a contract test keeps the
# two in step). Named here because config sits below the tool layer.
type VerifyCommand = Literal["pytest", "ruff_check", "ruff_format_check", "mypy"]


class VerifyConfig(Frozen):
    """STATIC_ANALYSIS (ADR-015): deterministic checks on the head tree, in this order. Each
    runs over the whole worktree. Hard preference checks always run as well. Tests do not
    run here: UNIT_TESTING and INTEGRATION_TESTING run them, and judge their failures."""

    commands: tuple[VerifyCommand, ...] = ("ruff_check", "ruff_format_check", "mypy")

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("[verify].commands lists a command twice")
        if "pytest" in self.commands:
            raise ValueError(
                "[verify].commands may not run pytest: tests run in UNIT_TESTING and "
                "INTEGRATION_TESTING, where their failures are triaged"
            )
        return self


class AgentsConfig(Frozen):
    """Bounds on one agent invocation, on top of its effect's budget reservation."""

    planner_max_turns: PositiveInt = 30
    coder_max_turns: PositiveInt = 60
    tester_max_turns: PositiveInt = 60
    triage_max_turns: PositiveInt = 20
    reviewer_max_turns: PositiveInt = 40  # a review, or a reply to the author's moves
    author_max_turns: PositiveInt = 20  # the Coder answering findings, read-only
    repro_max_turns: PositiveInt = 30  # the Tester writing one repro test
    arbiter_max_turns: PositiveInt = 4  # no tools: one answer, plus any pause_turn
    repo_map_max_paths: PositiveInt = 400  # files listed in the Planner's prompt


class TestingConfig(Frozen):
    """TESTING (PLAN §6.1): how failures are told apart."""

    __test__ = False  # not a pytest test class
    flake_reruns: PositiveInt = 3  # each failing test, rerun alone: mixed results are flaky
    # Where the Tester writes repro tests that settle disputes (ADR-004). One of its own
    # directories; its tests need not fail on the base tree, only on the disputed head.
    repro_directory: str = "tests/repro"


class AdmissionConfig(Frozen):
    """CREATED (ADR-015): what a run may ask for before any work starts."""

    max_budget_usd: PositiveFloat = 50.0
    max_wall_clock_minutes: PositiveInt = 240
    # Live runs of one repository at a time, parked ones included: each holds worktrees and
    # may be waiting on a person.
    max_live_runs: PositiveInt = 4


class IntegrationConfig(Frozen):
    """INTEGRATION_TESTING (ADR-015): the repository's integration suite."""

    # Where it lives. UNIT_TESTING leaves these out; this stage runs only these.
    paths: tuple[str, ...] = ("tests/integration",)
    # In a network namespace of its own (`unshare -rn`), so a test cannot reach any host.
    # auto: when the kernel allows it, and the report says whether it did; always: block
    # the stage where it cannot; never: only the sandbox's dead proxy stands in the way.
    isolate_network: Literal["auto", "always", "never"] = "auto"


class SecurityConfig(Frozen):
    """SECURITY_REVIEW (ADR-015): deterministic rules over what the change adds."""

    # A change to a dependency manifest is always listed in the report and the PR. With
    # this set, it also fails the gate: a person adds dependencies, not an agent.
    forbid_new_dependencies: bool = False
    # pip-audit, run when a change adds pinned Python dependencies (requirements files,
    # == pins in pyproject.toml, uv.lock, poetry.lock, Pipfile.lock): a known vulnerability
    # in one blocks. The harness adds pip-audit's arguments (--no-deps, so nothing is
    # installed). If it cannot run (not installed, no network), the report says why, without
    # blocking. []: no audit, and the report says so.
    audit_command: tuple[str, ...] = ("pip-audit",)
    audit_timeout_seconds: PositiveFloat = 120.0


class ReleaseConfig(Frozen):
    """FINAL_VERIFICATION and publishing (ADR-015)."""

    branch_prefix: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9/_-]*/$")] = "aegis/"


class PublishConfig(Frozen):
    """What happens to an approved release after it is committed to its local branch
    (ADR-018). ``msa approve --publish`` overrides ``mode`` for one approval.

    The push uses git's own credentials (a credential helper or SSH key). The pull request
    uses ``gh`` when it is installed and logged in, otherwise the REST API with the token in
    the variable ``token_env`` names; the token is never written anywhere."""

    mode: PublishMode = PublishMode.LOCAL  # local: nothing leaves this machine
    remote: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]+$")] = "origin"
    # owner/name on GitHub; empty: read from the remote's URL.
    repository: Annotated[str, StringConstraints(pattern=r"^([\w.-]+/[\w.-]+)?$")] = ""
    base: str = ""  # the pull request's base branch; empty: the repository's default branch
    token_env: HarnessVariable = "GITHUB_TOKEN"
    api_url: Annotated[str, StringConstraints(pattern=r"^https?://[^\s]+$")] = (
        "https://api.github.com"
    )
    timeout_seconds: PositiveFloat = 60.0  # per git push, gh or API call


class ServiceConfig(Frozen):
    """``msa serve`` (ADR-015 §3): the HTTP API."""

    host: str = "127.0.0.1"  # loopback by default: expose it deliberately, behind TLS
    port: Annotated[int, Field(ge=1, le=65535)] = 8765
    # Origins a browser may call the API from, e.g. the workbench's dev server.
    cors_origins: tuple[Annotated[str, StringConstraints(pattern=r"^https?://[^/\s]+$")], ...] = ()


class ObservabilityConfig(Frozen):
    """Traces (ADR-015 §3): one span per effect, invocation and tool call. ``console``
    writes finished spans to stderr; ``none`` makes them free no-ops."""

    traces: Literal["none", "console"] = "none"


class TemporalConfig(Frozen):
    """Driving runs with Temporal (ADR-017). With no ``address``, runs are driven in the
    process that starts or answers them; with one, ``msa worker`` drives them, and answers
    reach a run's workflow as signals."""

    address: str | None = None  # host:port of a Temporal frontend, e.g. localhost:7233
    namespace: str = "default"
    task_queue: str = "aegis"


class ReviewConfig(Frozen):
    """REVIEWING and NEGOTIATING (PLAN §4, ADR-014)."""

    # false: no Reviewer; every run is reviewed by a pass-through stand-in that the report
    # names. Arm B of the ablation (PLAN §11) runs this way.
    enabled: bool = True


class MsaConfig(Frozen):
    models: ModelsConfig
    api: ApiConfig
    pricing: dict[ModelId, Pricing]
    limits: LimitsConfig = LimitsConfig()
    journal: JournalConfig = JournalConfig()
    capabilities: CapabilitiesConfig = CapabilitiesConfig()
    sandbox: SandboxConfig = SandboxConfig()
    verify: VerifyConfig = VerifyConfig()
    testing: TestingConfig = TestingConfig()
    review: ReviewConfig = ReviewConfig()
    agents: AgentsConfig = AgentsConfig()
    admission: AdmissionConfig = AdmissionConfig()
    integration: IntegrationConfig = IntegrationConfig()
    security: SecurityConfig = SecurityConfig()
    release: ReleaseConfig = ReleaseConfig()
    publish: PublishConfig = PublishConfig()
    service: ServiceConfig = ServiceConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    temporal: TemporalConfig = TemporalConfig()

    @model_validator(mode="after")
    def _repro_tests_are_the_testers(self) -> Self:
        owned = self.capabilities.owned_directories(Role.TESTER)
        if self.testing.repro_directory not in owned:
            raise ValueError(
                f"[testing].repro_directory {self.testing.repro_directory!r} must be one of the "
                f"tester's own directories {list(owned)}"
            )
        return self

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
