# ADR-010: Preferences: field-level layers, sound conflict detection, checks before tokens

- **Status:** Accepted (2026-11-07)
- **Refines:** PLAN §5 (the preferences skill)

## Context

Preferences decide style disputes for every agent, so they must be predictable and
explainable:

- the same files always get the same rules, rendered byte-for-byte the same;
- every rule can say which layer it came from;
- contradictions surface before any money is spent;
- nothing changes without the owner's say.

PLAN §5 fixed the layers and the outline. This record settles the details that the outline
left open.

## Decision

1. **Layers hold patches, merged field by field.**
   - An entry names an `id` and only the fields it changes. `--pref team.x=off` therefore
     needs no copy of the rule.
   - The merge is a right-biased union, first by id and then by field. It is associative,
     the empty layer is its identity, and it is idempotent. Hypothesis checks all three
     laws, plus right bias.
   - After merging, each id must form a complete preference. Otherwise the error names
     the missing fields and the layers that defined the id.
   - Provenance records which layer set each field.
2. **`off` is a value, not a deletion.** A higher layer switches a preference off by
   setting `enforcement: off`, so disabling one keeps the merge a plain union.
3. **Hard conflicts are refused at INTAKE, soundly.**
   - Two `hard` preferences conflict when either declares `conflicts_with` the other and
     their scopes may overlap.
   - Scope overlap is checked conservatively. Languages must be compatible, and glob
     pairs must share a literal prefix and suffix relation. That test can report an
     overlap that doesn't exist, which the owner settles by narrowing a scope. It can
     never miss a real one: a property test checks that every path matching both scopes
     is reported, and the test fails when the prefix rule is weakened.
   - The INTAKE executor turns the conflict into `IntakeRejected`. The message names
     both preferences and the files they came from, and offers three remedies (I-16).
4. **Checks run before any LLM sees the code** (PLAN §5.4).
   - A `ruff` check runs `ruff check --isolated --select <codes>`. `--isolated` stops the
     project's ruff config from quietly disabling the owner's rule; an inline `# noqa` is
     still honoured, because it is deliberate and visible.
     Numeric rule prefixes (such as `E7`) select and attribute all matching codes,
     just as Ruff's `--select` does; unknown selectors fail the check.
   - A `regex` check runs through ripgrep, so a pattern cannot hang the run. Patterns
     that ripgrep's engine cannot run (lookaround, backreferences) are refused when the
     preferences load, not when a check runs.
     Loading validates against ripgrep's default engine with config disabled and empty
     stdin, under a five-second timeout; successful validations are cached. Ripgrep must
     be installed. Patterns must also compile in Python, which checks the rule's examples.
   - Checkers fail closed: a ruff run that produces no report is an error, not "no
     findings".
5. **Files are read safely and written faithfully.**
   - YAML is loaded with a safe loader that rejects duplicate keys; PyYAML would
     silently keep the last one.
   - `msa prefs add` and `msa prefs review --approve` write in round-trip mode, keeping
     comments and layout, and atomically.
   - Every write is resolved together with all the other layers first, so the CLI can
     never save a set that would fail INTAKE.
6. **Learned preferences wait for approval.** Proposals sit in `.msa/proposals/` until
   `msa prefs review --approve`, and they keep `source: learned` after approval.
7. **A shared glob module.** Capabilities (ADR-006) and preference scopes use one glob
   language. `msa.globs` sits below every layer that matches paths: `skills` and `tools`
   are siblings that may not import each other, and core's purity allowlist has no `re`.

## Verification

- **Merge laws:** `tests/unit/skills/test_merge_laws.py`.
- **Overlap soundness:** for globs (`tests/unit/test_globs.py`) and for whole scopes.
- **Rendering:** stays byte-stable when layer order and file order are shuffled.
- **I-16** (`tests/integration/test_preferences.py`): a journaled run with the real intake
  executor, a real git repository, and conflicting user and repo layers. It ends `FAILED`
  with reason `INTAKE_REJECTED` and the full message, and no effect after intake runs. A
  `--pref ...=off` override lets the same repository through to `DONE`.
- **Every `msa prefs` subcommand**, against real files, including its refusals.

## Consequences

- Patches let a layer change one field, but the meaning of "the same preference" now
  depends on ids staying stable across layers. Renaming an id in a lower layer orphans
  higher layers' patches; resolution reports them as incomplete.
- Selection by the plan's `files_expected` and the rendered block's place in the system
  prompt land with the agents in M6.

## Rejected

- **Whole-entry replacement per id.** It is simpler, but switching off a built-in would
  mean copying its full text into every layer that disables it.
- **Exact glob-intersection emptiness.** It is costly in general. A sound approximation
  is enough when its only failure mode is asking the owner to narrow a scope.
- **PyYAML.** Silently resolving duplicate keys hides real mistakes in a policy file, and
  it cannot round-trip comments.
- **Python's `re` for regex checks.** A user-supplied pattern can backtrack
  catastrophically, and lint and the checker would disagree on what a pattern means.
