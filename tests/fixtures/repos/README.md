# Fixture repositories

Tiny repositories for integration tests (PLAN §6.2). A test copies one into a temporary
directory and commits it (`tests.support.repos.make_repo`), so the fixture itself is never a
git repository and never modified.

- `greeter/`: one module and one test. I-01 adds a function to it; I-02 does the same with a
  lint error first.
