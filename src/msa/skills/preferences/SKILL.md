---
name: preferences
description: Apply the repository owner's coding preferences (layered, scoped rules with hard/soft/info enforcement) when writing or reviewing code. Use whenever code is written, edited or reviewed in a repository that has .msa/preferences.yaml or ~/.config/msa/preferences.yaml.
---

# Coding preferences

A preference is a rule about how the owner wants code written, with a scope and an
enforcement level. Preferences come in layers, lowest first:

1. built-in defaults (`defaults.yaml`, beside this file);
2. the user's global file, `~/.config/msa/preferences.yaml`;
3. the repository's `.msa/preferences.yaml`;
4. per-run overrides, `--pref <id>=hard|soft|info|off`.

A higher layer overrides a lower one by `id`, field by field. `enforcement: off` switches
a preference off.

## Applying them

- **hard**: a blocker. Do not write code that breaks it. When reviewing, a violation is a
  blocking finding of kind `preference`.
- **soft**: a nit. Follow it; when reviewing, report a violation as a non-blocking nit.
- **info**: context. Let it inform choices; never report it as a finding.
- A preference applies only to files matching its `scope` (`language` and path globs).
- A preference with a `check` is enforced by a tool before review. Do not spend effort
  re-checking it by hand, and do not argue with the tool's result.
- When two preferences seem to disagree, the more specific scope wins. If neither is more
  specific, say so instead of picking one. Two hard preferences that contradict each other
  where both apply are refused when the preferences are loaded.

## Schema

```yaml
version: 1
preferences:
  - id: py.errors.no-bare-except        # dotted, lowercase
    scope: { language: python, paths: ["src/**"] }
    rule: "Never use bare `except:`; catch the narrowest exception type."
    rationale: "Bare excepts swallow KeyboardInterrupt and hide bugs."
    enforcement: hard                   # hard | soft | info | off
    check: { kind: ruff, rule: E722 }   # or { kind: regex, pattern: "..." }
    examples: { good: "except FileNotFoundError:", bad: "except:" }
    conflicts_with: [py.errors.catch-all-at-top]
```

## Changing them

Preferences never change silently. Learned preferences are written to
`.msa/proposals/` and take effect only after `msa prefs review --approve <id>`. Inspect
the effective set with `msa prefs list`, `msa prefs show <id>`, `msa prefs explain <path>`
and `msa prefs diff-layers`.
