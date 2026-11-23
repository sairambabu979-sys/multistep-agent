# ADR-013: Evidence integrity: tests that ran, configuration the Coder cannot touch, checks on the delivered tree

- **Status:** Accepted (2026-11-18)
- **Refines:** ADR-012 (decisions 2 and 4), ADR-011 (the run worktree)

## Context

A review of M7 drove whole runs through the real executors with a scripted model, in which
the Coder ships a `farewell` that returns "bye ada" when the plan says "goodbye ada". A
control run caught it. Five variants finished `DONE`:

| Probe | How the wrong change passed |
|---|---|
| The Tester marks its test `@pytest.mark.skip` | TESTING counted a skipped test as passing. On the base tree the module failed to import, so fail-before held too. |
| The acceptance module defines no tests | "Acceptance tests exist" meant "a `.py` file changed under `tests/acceptance/`". |
| The Coder adds a `tests/conftest.py` that skips them | The Coder owns every path but the Tester's, and a conftest above a test runs inside its session. |
| The Coder adds `--ignore=tests/acceptance` to pyproject | pytest's configuration is Coder-owned too. |
| The code imports a gitignored `local_settings.py` | Checks ran in a worktree that held a file the tree, and so the patch, did not. A clean checkout of the delivered tree fails. |

The review also found that the weakened-test guard did not see a new skip or xfail mark, an
early `return`, or `assert True`, and that an acceptance module that fails to import on the
head tree was rerun under a node id pytest does not know. That read as an environment
failure, which ends the run, when it is the most ordinary `impl_bug` there is (the Coder did
not write what the plan named).

A further experiment showed that the problem is wider than conftest files *above* the
tests. pytest hooks such as `pytest_collection_modifyitems` are global within a session: a
conftest.py in `tests/unit/`, which the Coder may legitimately write, sees and can skip
every item of a whole-suite run, the Tester's included.

## Decision

1. **An acceptance test is evidence only if it ran.**
   - The Tester's test modules are the changed files under its directories that pytest
     collects by default (`test_*.py`, `*_test.py`). There must be one, and each must
     define a test, found in its source as pytest finds tests (`test*` functions, `test*`
     methods of `Test*` or `TestCase` classes).
   - Every test so found must appear in the head-tree run with outcome *passed*.
     Skipped, xfailed and never-collected tests are the Tester's to fix, by rule and
     without a model.
   - Fail-before is now positive: each test must *fail* on the base tree (a collection
     error counts). Skipped there, or not run there, is rejected like passing there.
   - Outcomes come from the JUnit report's `skipped` elements, not from the absence of a
     failure. A module-level case (a collection error, a module skipped whole) has the
     module's path as its node id, which is also what pytest accepts to rerun it.
2. **The acceptance tests run in a pytest session of their own.** The repository's tests
   run with this run's acceptance modules `--ignore`d; the acceptance modules then run
   alone, and so do their flake reruns. Nothing else in the suite (another module's import
   side effects, another directory's conftest.py) shares their session.
3. **The Coder may not change what decides how they run.** VERIFYING fails a change to
   any of these files in a directory above the Tester's (for `tests/acceptance`: the root
   and `tests/`):
   - `conftest.py`, every byte of it;
   - `pytest.ini` and `.pytest.ini`;
   - the parts of `pyproject.toml` (`[tool.pytest*]`, `[project.entry-points.pytest11]`),
     `tox.ini` (`[pytest]`) and `setup.cfg` (`[tool:pytest]`) that pytest reads. A
     dependency added to pyproject.toml is not a change to pytest.

   Creating, changing or deleting one is refused, with the reason, and goes back to the
   Coder like any failed check. The Tester's own conftest.py, inside its directories, is
   the Tester's.
4. **Checks run on exactly the delivered tree.** Before VERIFYING and TESTING run
   anything, `RunWorktree.prune` removes every file the next snapshot would leave out,
   ignored files above all, and the report lists them. Kept: `.venv/`, which uv builds
   from the lockfile, and the checks' cache directories. Sourceless `*.pyc` files are not
   kept, since one next to the sources is importable. `.venv/` is also never part of a
   tree, whether or not the environment ignores itself. The scratch worktree for
   fail-before is pruned the same way.
5. **The weakened-test guard counts live checks and sees marks.** A check after an
   unconditional `return`, `raise`, `continue`, `break`, `pytest.skip()`, `pytest.xfail()`
   or `pytest.exit()` is dead, and so is one under a constant-false condition. An assert
   that cannot fail is not a check: a truthy constant, a non-empty literal
   (`assert (x, "msg")`), or `x == x`. A Tester revision that adds a `skip`, `skipif`,
   `xfail`, or unittest skip mark to an existing test, on the test, its class or its
   module, is weakening. Reachability is judged conservatively: when unsure, a check is
   live.
6. **The prompts say so.** The Tester is told every test must run and must not be skipped
   or xfailed. The Coder is told pytest's configuration is not its to change, and that
   only tracked files are delivered.

## Verification

- `tests/integration/test_evidence.py`: each probe above, plus the hooks-are-global
  case, the module that does not import, an xfailed test, a test outside a test module,
  and a Tester revision that skips a flaky test. All ten finish `DONE` (or, for the import
  case, `ENVIRONMENT_FAILURE`) on the code before this record and are caught by it. The
  skipped-test case follows the run on: once the test runs, the wrong `farewell` fails
  it, triage sends it to the Coder, and the fixed run ends `DONE`.
- `tests/unit/runtime/test_testing.py`: the evidence rules on synthetic outcomes, one
  case per before/after combination.
- `tests/unit/runtime/test_guard.py`: live checks, vacuous asserts, marks, and the
  configuration comparison, including the edits it must ignore.
- `tests/unit/runtime/test_worktree.py`: `prune` leaves exactly the tree plus the
  environment and caches, and does not change the tree.

## Consequences

- A task whose point is to change pytest's configuration (register a marker, add a
  plugin) cannot finish until the Reviewer (M8) can approve such a change. That is
  deliberate: until then nobody can tell such a change apart from switching tests off.
- Fixtures the Coder's own tests share must live in their module, or in a conftest.py of a
  directory that is not above the Tester's.
- Each TESTING pass runs pytest at least three times (the repository's tests, the
  acceptance tests, and the acceptance tests on the base) instead of two. The acceptance
  run is small.
- A repository that configures `python_files`, `python_classes` or `python_functions`
  away from pytest's defaults will see its acceptance tests reported as not run. Reading
  those settings from the base tree's configuration is possible, if a fixture repo needs
  it.
- **What this does not close.** These are structural guards against a model taking a
  shortcut, not a security boundary (ADR-009 says the same of the sandbox):
  - Code under test can detect pytest, and a Coder's test executes arbitrary code that
    could write anywhere the sandbox allows, `.venv/` included.
  - `setup.py` entry points are not inspected.

  The Reviewer reads the diff for the first. An untrusted repository needs a container
  for the second.

## Rejected

- **`--confcutdir` for the acceptance run.** It would stop every conftest above the
  Tester's directories from loading, including the repository's own fixtures that the
  acceptance tests may rightly use. Refusing *changes* keeps those fixtures and still
  denies the Coder the lever.
- **Denying the files by capability.** pyproject.toml must stay writable for
  dependencies, and the files to deny depend on where the Tester's directories are. A
  check on the diff can compare only the sections pytest reads, and say why it refused.
- **Checking in a fresh checkout of the tree.** It is exact, but it rebuilds the
  environment for every check. Pruning gives the same bytes, keeps the environment, and
  can report what it removed.
- **Asking the triage model whether a skipped test counts.** It is a fact the report
  already holds.
