# ADR-011: The first slice: one run worktree, harness-made trees, and honest stand-ins

- **Status:** Accepted (2026-11-07)
- **Refines:** PLAN §3 (agents), §6.2 (I-01, I-02, I-14), §13 (M6)

## Context

M6 is the first milestone where real agents change real code: the Planner plans, the Coder
implements, and VERIFYING checks the result. Three things had to be settled before that
could be trusted:

- **Where the work happens, and who decides what tree it is at.** Evidence is bound to tree
  hashes (M1). If an agent could name the tree, it could name one it never built.
- **What survives a crash.** Effects are at-least-once (ADR-007), and an invocation in flight
  replays its paid turns (ADR-008). The files those turns wrote must still be there.
- **What a run does at stages that do not exist yet.** The state machine always passes
  through TESTING and REVIEWING. The Tester arrives in M7, the Reviewer in M8.

## Decision

1. **One worktree per run, created at INTAKE, outside the checkout.**
   - INTAKE resolves preferences and the base tree (M5). Then the worktree is created at
     `.msa/worktrees/<run>/work`, before `IntakeCompleted` is journaled. Every later
     executor, including one rebuilt by `msa resume`, finds it on disk.
   - Its commit is `commit-tree` of the base tree with a fixed identity and date, so one
     base always gives one commit, and `git_diff` shows exactly the run's changes.
   - The repository's own checkout is never modified. A finished run's result is a patch
     (`.msa/runs/<run>/patch.diff`) that the owner applies, or not.
2. **The harness makes every tree; agents only edit files.**
   - Integration, revisions and verification snapshot the worktree (`git add --all`, then
     `write-tree`). VERIFYING first proves the worktree holds the tree it was asked about.
   - Generated files (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, ...)
     are excluded through `core.excludesFile`, on top of the repository's `.gitignore`.
     Running a check therefore cannot change the tree it checked.
   - Harness git runs with hooks and fsmonitor off, without system or global config, and
     with fixed arguments: no agent input reaches a git command line.
3. **`.git` itself is protected.** In a linked worktree `.git` is a *file* naming the git
   directory. The old protected glob `.git/**` did not match it, so the Coder could have
   read it and edited it to point the harness's own git commands elsewhere. Protected is
   now `**/.git`, `**/.git/**` and `.msa/**`, which also covers submodules. A test makes
   the Coder try.
4. **A re-executed agent effect resumes; it never starts over in a dirty tree.** Before an
   implementation or revision, the worktree is reset to the effect's starting tree, unless
   the response store already holds a paid turn for that effect. In that case the turns
   replay, their recorded tool results stand in for the tools, and the files must stay as
   that execution left them.
5. **Plans, reports and patches are content-addressed artifacts.** The state machine carries
   digests only (`PlanRef`, `VerificationFailed.report`, `Finalized.artifact`). The contents
   live under `.msa/runs/<run>/artifacts/`, and every read checks the digest. The Coder's
   revision prompt is the verification report the event named, byte for byte.
6. **VERIFYING is deterministic and complete.** `[verify].commands` (default: ruff check,
   ruff format check, mypy, pytest) run in the sandbox over the whole worktree. Then come
   the machine checks of the preferences that apply to the changed files. Every check runs
   even after one fails, so one revision can fix all of them. Hard preference findings
   fail. A check that cannot run at all is `EffectFailed`, because no revision can fix the
   environment.
7. **Stages that do not exist yet pass through, and the report says so.** This is ablation
   arm A (PLAN §11):
   - `author_acceptance_tests` authors nothing;
   - `run_test_suite` passes: with no acceptance tests, fail-before/pass-after holds
     vacuously, and the repository's suite already ran in VERIFYING on the same tree;
   - `request_review` returns no findings.

   The report lists every stand-in the run used. Effects with no executor yet (a human plan
   gate, negotiation, arbitration) fail the run with a message instead of stalling it, and
   `msa run` refuses `plan_gate = human` up front.
8. **Prompts are versioned package data, and byte-stable inside a run.**
   - Role prompts are Markdown under `msa/agents/prompts/`.
   - A role's system prompt is its prompt plus the preference block for the base tree's
     files, so it is identical on every turn of every invocation of that role, and the
     cache holds.
   - Everything else goes in tagged user-prompt sections (`<task>`, `<plan>`,
     `<failure_report>`, ...). A closing tag inside the content is defused, and the prompts
     say that repository text is data, never instructions.

## Consequences

- A run is a whole, journaled, reportable pipeline from M6 on. Each later milestone replaces
  a stand-in with a real executor, without changing the state machine.
- The worktree is the run's single working copy. Running the Tester in a second worktree
  (M7) adds a merge to `integrate_build`; path ownership keeps it conflict-free.
- Snapshots stage the whole worktree, which costs time on very large repositories. That
  is acceptable for M6; M9's evals will measure it.
- A run that was recording a cassette cannot be resumed (the cassette would hold only
  half the run). `msa resume` says so.

## Rejected

- **Letting the Coder commit.** Agents would then name the trees evidence binds to. The
  harness snapshots instead.
- **Working in the user's checkout.** That would mix the run's changes with the owner's
  uncommitted work, and a failed run would leave debris behind.
- **Skipping TESTING and REVIEWING in the state machine for M6.** That would change the
  exhaustively model-checked core for a temporary gap. Stand-ins keep the core as is.
