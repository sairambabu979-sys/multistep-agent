# Fixture repositories

Tiny repositories for integration tests (PLAN §6.2). A test copies one into a temporary
directory and commits it (`tests.support.repos.make_repo`), so the fixture itself is never a
git repository and never modified.

- `greeter/`: one module and one test. I-01 adds a function to it; I-02 does the same with a
  lint error first.
- `inventory/`: a small package of three modules, unit tests, an integration suite and a
  repository preference layer (`.msa/preferences.yaml`, which makes `py.output.no-print`
  hard). The definition-of-done run (`tests/integration/test_demo.py`) changes four of its
  files.
