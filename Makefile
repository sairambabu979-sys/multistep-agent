.DEFAULT_GOAL := check
.PHONY: sync fmt lint types imports fsm test cov check nightly chaos mutation changelog docs demo

sync:     ## Install the locked environment
	uv sync --locked

fmt:      ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

lint:     ## Lint and format check
	uv run ruff check .
	uv run ruff format --check .

types:    ## mypy --strict over src and tests
	uv run mypy

imports:  ## Architecture (layering) contracts
	uv run lint-imports

fsm:      ## Static analysis of the transition table
	uv run msa fsm check

test:     ## Full test suite
	uv run pytest

cov:      ## Tests with coverage gates (85% overall, 95% core)
	uv run pytest --cov --cov-report=term-missing
	uv run coverage report --include='src/msa/core/*' --fail-under=95

check: lint types imports fsm cov  ## Everything CI runs

nightly:  ## 10k examples per property, 100 crash trials, mutation testing (slow)
	HYPOTHESIS_PROFILE=nightly uv run pytest tests/property tests/model_based tests/unit
	MSA_CHAOS_TRIALS=100 uv run pytest tests/chaos
	$(MAKE) mutation

chaos:    ## Crash-recovery trials (MSA_CHAOS_TRIALS, default 25)
	uv run pytest tests/chaos

mutation: ## Mutation-test msa.core; fails if any mutant survives
	rm -rf mutants
	HYPOTHESIS_PROFILE=mutation uv run mutmut run
	uv run mutmut export-cicd-stats
	uv run python scripts/mutation_gate.py || (uv run mutmut results; exit 1)

changelog: ## Regenerate CHANGELOG.md from conventional commit subjects
	uv run git-cliff --config pyproject.toml --output CHANGELOG.md

demo:     ## Simulate a run against stub executors, then list the journal
	uv run msa simulate --seed 11
	uv run msa runs

docs:     ## Regenerate generated docs: the state diagram and the msa.toml schema
	uv run msa fsm diagram --output docs/fsm.md
	uv run msa config schema --output schemas/msa.schema.json
