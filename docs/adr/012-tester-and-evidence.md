# ADR-012: The Tester: blind tests, evidence rules decided by the harness, and owners for every failure

- **Status:** Accepted (2026-11-07)
- **Refines:** PLAN §6.1 (what the Tester does at runtime), ADR-011 (the stand-ins)

## Context

Tests written by the same process that wrote the code tend to inherit its mistakes. PLAN
§6.1 asks for five safeguards: blind authoring, fail-before/pass-after, flake detection that
never skips, triage to the right owner, and a guard against weakened tests. M7 had to turn
those into mechanisms, and settle three questions the outline left open:

- **Who decides what a test run means?** A model can be asked whether a test is flaky or
  whether it passes before the change, but both are facts a harness can establish.
- **Where do failures in the Tester's files go?** VERIFYING runs before TESTING, and its
  failures always go to the Coder (the state machine says so). The Coder may not edit the
  Tester's paths (ADR-006).
- **How is a weakened test told apart from a corrected one?**

## Decision

1. **Blind by construction.** The Tester works in its own worktree
   (`.msa/worktrees/<run>/tests`), created at INTAKE at the base tree, in parallel with the
   Coder. It reads the base, never the implementation. `integrate_build` overlays the
   Tester's directories from its tree onto the Coder's tree. Path ownership guarantees nobody
   else changed them, so the overlay cannot conflict and is idempotent. After integration
   both roles work in the build worktree, each limited by its capability.
2. **Facts are established by running, not by asking.** TESTING, in order:
   - **Acceptance tests exist.** No new test files under the Tester's directories is a
     `test_bug`.
   - **They are clean.** ruff check, ruff format and the hard preference checks run on the
     Tester's files here, not in VERIFYING (decision 3).
   - **Pass-after.** The whole suite runs on the head tree.
   - **Flakes.** Each failing test is rerun alone up to `[testing].flake_reruns` times
     (k = 3). Mixed results make it `flaky`: it is reported, and goes back to the Tester. It
     is never skipped or quarantined.
   - **Fail-before.** The acceptance tests run against the base tree in a scratch worktree.
     A test that passes there is rejected (I-03). A collection error on the base counts as a
     failure, which is the usual way a test of new code fails.
   - **Triage.** Only consistent failures go to a model: the Tester role, read-only, answers
     `impl_bug` (to the Coder), `test_bug` (to the Tester) or `environment` (the run fails;
     no agent can fix it).

   Problems a rule can name (flaky, rejected, unclean, missing) are resolved first, without
   paying a model. Outcomes are read from pytest's JUnit XML, written outside every
   worktree. A suite that cannot run at all is an environment failure.
3. **VERIFYING leaves the Tester's directories to TESTING.** ruff, mypy, pytest and the
   preference checks in VERIFYING exclude `tests/acceptance` and `tests/repro` (the
   directories the Tester owns outright, read from `[capabilities]`). Otherwise a lint error
   in an acceptance test would send the Coder to fix a file it may not touch, and a failing
   acceptance test would skip fail-before and triage. The state machine is unchanged.
4. **The weakened-test guard counts checks.** A Tester revision may not remove a test
   function, or lower its number of checks (`assert`, `pytest.raises`, `warns`, `approx`,
   `fail`). That would need a Reviewer finding approving it (M8); until then the effect fails
   with the list of what was weakened. Tests that TESTING rejected (they pass before the
   change) are exempt: replacing them is the fix. The count is structural, not a proof.
   `assert x == 1` → `assert x` keeps it, and that is the Reviewer's to catch.
5. **The test report is the revision request.** It holds the fail-before/pass-after matrix,
   every problem, the flake reruns and the triage. It is stored as an artifact, named by
   `TestsFailed.report`, and handed verbatim to whichever role revises. The run report
   shows the latest one.

## Consequences

- Arm B of the ablation (PLAN §11) is now real: Planner, Coder, Tester and VERIFYING. Only
  the review stage is a stand-in.
- A model is paid in TESTING only to diagnose a consistent failure.
- Flaky tests cost a revision instead of a silent retry. That is deliberate: a flaky
  acceptance test is not evidence.
- The suite runs several times per TESTING (head, reruns, base), which is slow on large
  suites. The full run could be narrowed to affected tests later, if M9 measures a need.
- I-15 (stale evidence) needed no new mechanism: the core rejects evidence for any tree but
  the head, and the interpreter fails the run when an executor offers it.

## Rejected

- **Asking the model whether a test is flaky, or passes on the base.** Both are facts that
  are cheaper and more reliable to establish by running the tests.
- **Routing VERIFYING failures by file owner.** That would change the state machine's
  VERIFYING outcome for a problem decision 3 avoids.
- **Quarantining flaky tests to keep runs green.** It hides exactly the failures acceptance
  tests exist to surface.
