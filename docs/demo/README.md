# The definition-of-done run

A snapshot of one run of `tests/integration/test_demo.py`. It is the run that shows the whole
platform working end to end: one multi-file task, every stage, a crash, and a PR for a person
to approve.

- [`pr.md`](pr.md): the PR description the Release Verifier wrote, which the approver read.
- [`timeline.txt`](timeline.txt): every journaled event, in order (`msa inspect run-1`).
- [`patch.diff`](patch.diff): the delivered change, against the fixture's base commit.

**The model was scripted.** No live model is used in tests, and this snapshot was made without
one. Each role's answers (Planner, Coder, Tester, Reviewer) come from a script in the test.
Everything else ran for real: git and the worktrees, the sandbox, ruff, pytest (the integration
suite inside a network namespace), the security rules, the journal and the response store on
disk, the crash, and the published branch. The test passes only if each stage reacts to the
scripted answers as asserted. A live run shows what a model actually writes; this run shows
what the platform does with it. The `live run` workflow (`.github/workflows/live-run.yml`)
runs the same task with the real model, crashes it, and resumes it. Its results are the
workflow's own, in its job summary and artifact, not this snapshot.

## What the run had to catch

The task changes four files of `tests/fixtures/repos/inventory`. The scripted Coder makes four
deliberate mistakes, and each one must be caught by the stage built for it:

| # | the mistake | caught by | how |
|---|---|---|---|
| 1 | `print()` in library code | STATIC_ANALYSIS | the repository's `.msa/preferences.yaml` makes `py.output.no-print` hard (ruff T201) |
| 2 | `restock()` accepts a quantity of 0 | UNIT_TESTING | the Tester's blind acceptance test fails; the Tester diagnoses `impl_bug` |
| 3 | `eval()` parses a quantity | SECURITY_REVIEW | the call, found in the syntax tree of the added lines; high severity, not negotiable |
| 4 | `receive()` is not atomic | CODE_REVIEW | the Reviewer's finding `c3-1`, which the Coder accepts in NEGOTIATION |

Each fix is a revision (four in all, the limit), and every gate runs again on each revised tree.

The crash is a real `SIGKILL`. It hits in the middle of the third revision, after the Coder
edited `receiving.py` and before its final answer. The run resumes from the journal:

- no effect whose completion was journaled runs again;
- the revision that was in flight runs again, and its turns are replayed from the response store
  rather than re-run, so the edit is not applied twice;
- the run ends with the branch `aegis/run-1`, committed with `Approved-by:`, and the
  repository's own checkout is left untouched.

## Regenerating

```bash
MSA_UPDATE_DEMO=1 uv run pytest tests/integration/test_demo.py
```

Hashes, digests and timings differ from run to run, so these files are not compared with
anything. The test's assertions are the check; the snapshot is for reading.
