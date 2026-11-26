# ADR-020: Audit the pinned Python dependencies a change adds, by default

- **Status:** Accepted (2026-11-26)
- **Refines:** ADR-015 (SECURITY_REVIEW)

## Context

SECURITY_REVIEW listed every dependency manifest a change touched. With
`[security].audit_command` set, it also ran that command and blocked on a non-zero exit.
The default was `[]`, so nothing was audited unless someone set it. A configured
`["pip-audit"]` would also have audited the wrong thing: the environment msa runs in, not
the change. And to resolve a requirements file it installs packages, running their build
hooks.

## Decision

1. **On by default.** `audit_command` defaults to `["pip-audit"]`, now a dev dependency of
   this project (pinned to the version `make audit` uses). `audit_command = []` turns the
   audit off, and the report says it is off.
2. **Only what the change adds, and only exact versions** (`msa.runtime.dependencies`).
   - **Read from files, never installed:** the head and base trees' `requirements*.txt` /
     `.in` files, `==` pins in `pyproject.toml` (dependencies, optional dependencies,
     dependency groups), `uv.lock` and `poetry.lock` index packages, and `Pipfile.lock`.
   - **An addition** is a (name, version) the head has and the base did not, compared under
     PEP 503 names: a new dependency, or a new version of one.
   - **Not audited, but listed:** a requirement with no exact version (`>=`, `~=`, a
     wildcard). It has no version to audit.
   - **Out of scope:** non-Python manifests (`package.json`, `Cargo.lock`...) are still
     listed, and the report says pip-audit does not cover them.
3. **One pip-audit call, that installs nothing:** `--no-deps --disable-pip --format json
   --requirement <the additions>`, with `[security].audit_timeout_seconds` (120). The harness
   adds these arguments, so `audit_command` names pip-audit, not an arbitrary command. It
   runs on this machine with its proxy and CA settings: it needs the network, and runs none
   of the repository's code (ADR-019).
4. **A known vulnerability in an addition blocks.** It is a high finding naming the package,
   version, advisory ids, and the versions that fix it. Like every security finding, it goes
   to the Coder, or to the Tester when it is in their files.
5. **An audit that cannot run does not block.** pip-audit missing, no network, a timeout,
   output that is not its JSON, a dependency file that does not parse: each is a medium
   finding that says why. It is in the report and the PR, and passes the gate. It says
   nothing about the change, and a blocking finding would send work to an agent that cannot
   fix the environment.

## Consequences

- With network access, a change that adds `requests==2.19.0` is stopped, with
  PYSEC-2018-28 and its fix in the report. Without access, the report says the audit did
  not run.
- A configured `audit_command` that was not pip-audit no longer works, since the harness
  now passes pip-audit's arguments. `["pip-audit", "--strict"]` and similar still do.
- Tests use a pip-audit stand-in that records its arguments and the requirements it was
  given. No test reaches PyPI.

## Rejected

- **Auditing the whole manifest:** a repository's existing advisories would block every run
  that touched the file, which is debt the change did not add (ADR-015 reviews what a change
  adds).
- **Resolving ranges to versions:** it means running a resolver, and often build hooks, on
  the repository's requirements.
- **Blocking when the audit cannot run:** every offline run that touched a dependency file
  would go back to the Coder, who cannot fix it, until the revision limit.
