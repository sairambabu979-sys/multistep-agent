# Changelog

All notable changes, generated from [Conventional Commits](https://www.conventionalcommits.org/)
by [git-cliff](https://git-cliff.org). Regenerate with `make changelog`; do not edit by hand.

## [Unreleased]

### Features

- **core:** M1 pure, event-sourced orchestrator state machine ([`ba94af4`](https://github.com/sairambabu979-sys/multistep-agent/commit/ba94af413dceed1c0a47b811031ce073587f4204))
- **core:** EffectFailed escape and fold() for resuming from snapshots ([`35aab3c`](https://github.com/sairambabu979-sys/multistep-agent/commit/35aab3cbad71669016f356aaa5ad8b4fb8ff7192))
- **runtime:** M2 journal, interpreter, simulator, and crash-recovery tests ([`51ecdc9`](https://github.com/sairambabu979-sys/multistep-agent/commit/51ecdc9f7fe50191e8f2cf65c8788b77995d6714))
- **config:** msa.toml holds every model id, effort, beta, price and limit ([`8058e8e`](https://github.com/sairambabu979-sys/multistep-agent/commit/8058e8e39feea063d365f88935f8b603f3490e61))
- **llm:** budgeted, replayable Messages API gateway (M3) ([`bc0b898`](https://github.com/sairambabu979-sys/multistep-agent/commit/bc0b8984ffabc85699dd815731bae2821bbd4b98))
- **cli:** msa llm ping, a recordable two-turn health check ([`59000fc`](https://github.com/sairambabu979-sys/multistep-agent/commit/59000fcbba20071b760f17c06468419c3d12b344))

### Documentation

- add engineering plan for the multistep agent ([`0e00677`](https://github.com/sairambabu979-sys/multistep-agent/commit/0e00677373f5c02be206b4249b67a052ec7dc217))
- record mutation testing, msa.toml and the changelog in PLAN and README ([`68c51fe`](https://github.com/sairambabu979-sys/multistep-agent/commit/68c51fe2ff772bd14fffd37eca40fe39b785ca41))
- ADR-008 for the gateway, and M3 status in PLAN and README ([`1b5408d`](https://github.com/sairambabu979-sys/multistep-agent/commit/1b5408d91542575f4a1b78159c4cfbc97cd307fa))
- **changelog:** regenerate for M3 ([`13334e2`](https://github.com/sairambabu979-sys/multistep-agent/commit/13334e283ae60c7b7f782393a0e27547cab56a1d))

### Tests

- **core:** mutation testing kills all 1,070 core mutants ([`22c4800`](https://github.com/sairambabu979-sys/multistep-agent/commit/22c4800f21b50b8bb3dd0a05e5957bda9d59e6aa))
- **cli:** make the budget-override test deterministic ([`eb4f6b9`](https://github.com/sairambabu979-sys/multistep-agent/commit/eb4f6b94a7ac98de26bb9bb5514bb060413e7d8b))

### Build and tooling

- M0 project skeleton, tooling, CI, and ADRs ([`94d1923`](https://github.com/sairambabu979-sys/multistep-agent/commit/94d19235d6d6d91120ce3aad105a60881cb16ab4))
- conventional-commit subject check ([`3d83448`](https://github.com/sairambabu979-sys/multistep-agent/commit/3d834486043f2cbf895d9470a46b81ed1021d9ac))
- wire mutation testing, changelog, config and commit checks ([`3480a03`](https://github.com/sairambabu979-sys/multistep-agent/commit/3480a03907883887a904de489dc1da8c2f089753))
- generate CHANGELOG.md, keeping subjects as written ([`72f8a37`](https://github.com/sairambabu979-sys/multistep-agent/commit/72f8a370c1869229cdfe13054010e6194b5d61fa))

