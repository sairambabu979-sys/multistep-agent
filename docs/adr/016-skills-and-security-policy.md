# ADR-016: Skills and the security policy: versioned, narrowing, and outranked by policy

- **Status:** Accepted (2026-11-23)
- **Refines:** ADR-010 (preferences), ADR-006 (capabilities), ADR-015 §1 (Preferences/Skills)

## Context

AEGIS asks for a skills registry: versioned skills with typed inputs and outputs, allowed
tools, constraints and validation; inheritance and overrides; edit, export and delete. It
also asks that security policies outrank preferences, and that no secret is ever stored.

Preferences (ADR-010) already say *how code should look*, are layered, and are checked by
machine. What they cannot express is *how a role should work*: the procedure for a
migration, the tools a role needs for it, and what it must hand back. They also had no
floor: any layer, a run's `--pref` flag included, could switch any preference off.

## Decision

1. **A skill is data, never code.** One YAML file per version,
   `.msa/skills/<id>/<version>.yaml` (repo) or `~/.config/msa/skills/...` (user). A skill
   carries a description, the roles that use it, instructions and constraints, the tools it
   allows, and typed inputs and outputs. A repo file overrides the same id and version in
   the user layer, and the registry records the file it replaced.
2. **Versions are immutable.** `msa skills add` refuses an existing version. An edit is a
   new version, and a role always uses the latest version of each skill that names it.
   A version that another skill extends cannot be deleted.
3. **Inheritance only narrows.** `extends: id@version` inherits every field left out.
   - A child's roles and tools must be among its parent's.
   - Inputs and outputs may be added, but never retyped.
   - Instructions and constraints accumulate, parent first.

   A child can therefore drop a capability but never gain one. Cycles and missing parents
   are named.
4. **Skills reach a role in two places.** The role's system prompt gets a `# Skills` block
   after the preference block. It is byte-stable for the run, so the cached prefix holds
   (ADR-008). The role's toolset is narrowed to the intersection of what its skills allow.
   Skills are read from the user's repository and user layer, never from a worktree, so no
   agent can add one. (`.msa/` is protected from every role anyway, by ADR-006.)
5. **The security policy is a layer nothing outranks.** `.msa/policy.yaml` has two parts:
   - **Locks**, which set a preference's enforcement. A lock is applied after every
     preference layer, the run's `--pref` flags included. `msa prefs show` names the policy
     as the source, and a lock on an undefined preference is an error.
   - **Forbidden tools** per role. They are removed from that role's toolset whatever a
     skill allows, and a skill that grants one is refused.

   A lock never switches a rule off.
6. **What is refused, and when.** Everything below is checked when it is written (`msa
   skills add`, `msa prefs add`) and again in VALIDATING. So a broken or unsafe file stops
   a run before any model is paid:
   - a skill that does not resolve;
   - a writing tool for a role that writes no files;
   - a tool the policy forbids;
   - a secret in any skill, preference layer or policy text.

   Every refusal names what to fix, and none repeats the secret it found.
7. **Preference controls.**
   - `msa prefs export` writes the resolved set, policy applied, as one complete layer.
   - `msa prefs delete` removes an entry from a layer, keeping the file's comments. It is
     refused if the remaining layers would not resolve.

## Verification

- **The registry's rules**, each with its exact refusal (`tests/unit/skills/test_registry.py`):
  narrowing in both dimensions, retyping, cycles and missing parents, immutability,
  deletion of a used parent, numeric version order (1.10.0 after 1.9.0), user and repo
  override, write tools for read-only roles, policy over skills, typed I/O, the
  self-contained export, and the byte-stable rendering.
- **Policy over preferences** (`test_policy.py`): `--pref <id>=off` does not unlock a
  locked preference, provenance names the policy, and invalid policies are refused,
  including one holding a secret.
- **A whole live run** (`tests/integration/test_skills.py`):
  - the coder's skill reaches its system prompt and removes `git_diff` from its tools;
  - the policy removes `run` from the tester's tools;
  - roles without skills get no skills block;
  - a skill that grants a forbidden tool stops the run in VALIDATING, having spent nothing.

  Removing either tool filter by hand fails this test.
- **The command line** (`tests/unit/test_cli_skills.py`): add, list, show, export, check
  and delete; secret refusal for skills and preferences; `policy show`; `prefs export`
  and `prefs delete`.

## Consequences

- A skill changes what a model reads. Whether a given skill improves results is an eval
  question (M9), not one this registry answers. Skills ship empty: nothing is on by default.
- The tool intersection is strict: two skills for one role that allow disjoint tools
  leave it none but what both allow. That is least privilege, and `msa skills show` says
  which tools each skill allows.
- Policy is per repository. An organization-wide policy would be one more layer under it,
  with the same rule: a lower policy can only be tightened.

## Rejected

- **Skills as plugins (code).** They would run in the harness with its privileges. Text
  and a tool list give a model what it needs without giving it a new way to act.
- **Letting a child skill widen tools or roles.** Then inheriting from a narrow, reviewed
  skill would be a way around its review.
- **The policy in `msa.toml`.** `msa.toml` holds settings a run may change (`--budget-usd`).
  The policy holds what no run may change, so it gets its own file under the protected
  `.msa/`.
