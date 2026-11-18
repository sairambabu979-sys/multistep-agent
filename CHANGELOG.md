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
- **tools:** race-free path confinement, capabilities and the local sandbox (M4) ([`1d5e4d3`](https://github.com/sairambabu979-sys/multistep-agent/commit/1d5e4d35b87e72a1867be6597e77be4d11fdc2ba))
- **prefs:** the preferences skill: layered, scoped, conflict-checked (M5) ([`5a07425`](https://github.com/sairambabu979-sys/multistep-agent/commit/5a07425f7966ecb926a7a1cdf3a71a67a0cfdb01))
- **runtime:** real INTAKE, preference checks, and msa prefs (I-16) ([`1627deb`](https://github.com/sairambabu979-sys/multistep-agent/commit/1627deb3e29c3449146426493f508d10f56894e5))
- **agents:** the Planner and Coder: schemas, prompts and invocations (M6) ([`bee5d0e`](https://github.com/sairambabu979-sys/multistep-agent/commit/bee5d0e662a2e752a664ea15bc0014ee2df6c6d1))
- **runtime:** live runs with the Planner, Coder and VERIFYING; msa run (M6) ([`94b39c8`](https://github.com/sairambabu979-sys/multistep-agent/commit/94b39c8f106de8bae91f6800eeeb632391300950))
- **runtime:** the Tester: blind acceptance tests, fail-before/pass-after, flakes, triage (M7) ([`f2687d2`](https://github.com/sairambabu979-sys/multistep-agent/commit/f2687d2d62be39e974f76c06cb43a1f503cfcc41))

### Bug fixes

- **llm:** strict-mode schema transform and tool-result replay ([`33a285b`](https://github.com/sairambabu979-sys/multistep-agent/commit/33a285bf839d736ca6202fe9c446e4a06c340898))
- **tools:** refuse a path that changes while it is being resolved ([`92f567c`](https://github.com/sairambabu979-sys/multistep-agent/commit/92f567cf95998448f89a71101e029851aef988e0))
- **tools:** protect the .git file of linked worktrees and submodules ([`ff01412`](https://github.com/sairambabu979-sys/multistep-agent/commit/ff014128d4a21fb3cc9c4721f92ea407b875e61a))
- **runtime:** keep pytest from collecting TesterExecutor as a test class ([`d71bfc4`](https://github.com/sairambabu979-sys/multistep-agent/commit/d71bfc48544d9a2af046532a5d029a7fdc03650e))

### Refactoring

- **globs:** one glob language below every layer that matches paths ([`5b9092b`](https://github.com/sairambabu979-sys/multistep-agent/commit/5b9092b00f01a1859cea6d83b5f5fdaaa0726d94))

### Documentation

- add engineering plan for the multistep agent ([`0e00677`](https://github.com/sairambabu979-sys/multistep-agent/commit/0e00677373f5c02be206b4249b67a052ec7dc217))
- record mutation testing, msa.toml and the changelog in PLAN and README ([`68c51fe`](https://github.com/sairambabu979-sys/multistep-agent/commit/68c51fe2ff772bd14fffd37eca40fe39b785ca41))
- ADR-008 for the gateway, and M3 status in PLAN and README ([`1b5408d`](https://github.com/sairambabu979-sys/multistep-agent/commit/1b5408d91542575f4a1b78159c4cfbc97cd307fa))
- **changelog:** regenerate for M3 ([`cb9386a`](https://github.com/sairambabu979-sys/multistep-agent/commit/cb9386a889d7f2b0e2210df45cff6a18edeec14e))
- ADR-009 for the tool layer, and M4 status in PLAN and README ([`5b57976`](https://github.com/sairambabu979-sys/multistep-agent/commit/5b579762e70aabe929ecccc4555b143b9abca9d5))
- **changelog:** regenerate for M4 ([`7f3d3fe`](https://github.com/sairambabu979-sys/multistep-agent/commit/7f3d3fe84e60d02d0041f78fef839bf664b643d0))
- ADR-010 for preferences, and M5 status in PLAN and README ([`557ed9b`](https://github.com/sairambabu979-sys/multistep-agent/commit/557ed9b6db49165d7b9814226b2af00e3d4c0e68))
- **changelog:** regenerate for M5 ([`8acb6a5`](https://github.com/sairambabu979-sys/multistep-agent/commit/8acb6a54033bd695b449ef61d2e75d0578542a3d))
- ADR-011 for the first slice, and M6 status in PLAN and README ([`5dc6b89`](https://github.com/sairambabu979-sys/multistep-agent/commit/5dc6b892bdfd09cc627996dd8f004055628c9fa3))
- **changelog:** regenerate for M6 ([`612eef3`](https://github.com/sairambabu979-sys/multistep-agent/commit/612eef3740d30804f5602422b6547f2b735cdc60))
- mark I-01, I-02 and I-14 green in PLAN (CI run 42386339304) ([`a45f4ff`](https://github.com/sairambabu979-sys/multistep-agent/commit/a45f4ff652981f4cd7f90fd6c5e9391fbd023604))
- ADR-012 for the Tester, and M7 status in PLAN and README ([`f0c9039`](https://github.com/sairambabu979-sys/multistep-agent/commit/f0c90392b9b77c41c15973137f8057ba5a7b8af5))

### Tests

- **core:** mutation testing kills all 1,070 core mutants ([`22c4800`](https://github.com/sairambabu979-sys/multistep-agent/commit/22c4800f21b50b8bb3dd0a05e5957bda9d59e6aa))
- **cli:** make the budget-override test deterministic ([`eb4f6b9`](https://github.com/sairambabu979-sys/multistep-agent/commit/eb4f6b94a7ac98de26bb9bb5514bb060413e7d8b))
- **tools:** expect the requested file in permission errors ([`e16e5db`](https://github.com/sairambabu979-sys/multistep-agent/commit/e16e5dbeb26663dd42f6fb43e4ecfd2a2041b61a))

### Build and tooling

- M0 project skeleton, tooling, CI, and ADRs ([`94d1923`](https://github.com/sairambabu979-sys/multistep-agent/commit/94d19235d6d6d91120ce3aad105a60881cb16ab4))
- conventional-commit subject check ([`3d83448`](https://github.com/sairambabu979-sys/multistep-agent/commit/3d834486043f2cbf895d9470a46b81ed1021d9ac))
- wire mutation testing, changelog, config and commit checks ([`3480a03`](https://github.com/sairambabu979-sys/multistep-agent/commit/3480a03907883887a904de489dc1da8c2f089753))
- generate CHANGELOG.md, keeping subjects as written ([`72f8a37`](https://github.com/sairambabu979-sys/multistep-agent/commit/72f8a370c1869229cdfe13054010e6194b5d61fa))

