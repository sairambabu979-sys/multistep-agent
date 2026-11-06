# ADR-009: The tool layer: race-free confinement, capabilities on resolved paths, an honest local sandbox

- **Status:** Accepted (2026-11-06)
- **Amends:** ADR-008 decisions 3 and 8 (strict schemas; replay of tool results)

## Context

Agents read and write a real worktree and run its tests. The tool layer has to keep three
promises, whatever the model asks for and whatever the repository contains:

- **Confinement.** No tool reads, writes or lists anything outside the worktree (PLAN §8).
- **Ownership.** A role writes only its own paths, so the Coder cannot weaken the Tester's
  tests (ADR-006).
- **Bounded commands.** Commands are allowlisted, typed, time-limited and size-limited.

A naive implementation checks a path and then opens it by name. That is a race: a symlink
swapped in between the check and the open redirects the open anywhere.

## Decision

1. **Resolve once, then open without following.**
   - `Root.resolve` joins the requested path to the root and resolves it through symlinks
     (`realpath`). The result must stay inside the root. In-tree symlinks work; anything
     leaving the tree is `PathRejected`.
   - The resolved path is then opened one component at a time from a descriptor of the
     root, every step with `O_NOFOLLOW`. A symlink that appears after resolution fails the
     open (`PathChanged`).
   - The walk also refuses `..` and absolute parts on its own, so each layer holds even if
     the other had a bug. FIFOs are opened non-blocking and refused, as are devices and
     sockets.
2. **Capabilities are checked on the resolved path.** An in-tree symlink cannot launder a
   write into another role's files. On a case-insensitive disk, paths and globs are
   compared NFC-normalised and case-folded, so `Tests/Acceptance/` cannot dodge a deny.
   Globs live in `msa.toml` `[capabilities]`. A role's policy is fixed when its workspace
   is built, and no tool input names it.
3. **Writes are atomic and never write through.**
   - Every write goes to a temporary file in the same directory, is fsynced, and is
     renamed into place. That breaks hard links, so a write never reaches an inode shared
     with a path outside the tree.
   - New files are linked into place, which cannot replace a file that appeared meanwhile.
   - An edit must quote the `content_hash` from the read that showed the file
     (`StaleRead` otherwise). The capability check runs first, so a refusal reveals
     nothing about the file.
4. **Refusals are tool errors, and security refusals are also recorded.** A refusal comes
   back to the model as an error result and the run continues (I-12). Path escapes,
   symlink races and capability denials also produce a `Violation` record for the
   operator (I-13), logged to `msa.security` by default.
5. **Commands go through an allowlist with typed arguments.**
   - The model names `pytest`, `ruff_check`, `ruff_format_check` or `mypy`, plus confined
     paths. Paths are passed as `./path`, so none can parse as an option. It never sends a
     command line.
   - The environment is scrubbed to `[sandbox].env_passthrough`. Config validation refuses
     any name that looks like a credential.
   - Rlimits (CPU, file size, no core dumps) are set by a fixed trampoline that then
     `exec`s the command. `preexec_fn` is not safe once threads exist, and file tools do
     run on threads.
   - Each command runs in its own process group. The whole group is killed on timeout,
     and after every run, so nothing it started outlives the call.
   - Output keeps its head and tail; the middle is elided and counted.
6. **Search is ripgrep.** Its regexes run in linear time, so a hostile pattern cannot hang
   the run. It honours `.gitignore`, skips binary files and never follows symlinks.
   Results are sorted by path; see decision 8.
7. **`git_diff` cannot run repository-configured programs.** fsmonitor, hooks, external
   diff drivers and textconv are all disabled, and system and global config are ignored.
   Revisions that could parse as options are refused.
8. **Tool results are replayed with their turn (amends ADR-008 §8).** Real tool output is
   never byte-identical twice (test timings, for one). If a replayed turn re-ran its
   tools, the next request would differ, and the replay would stop there, paying again for
   every later turn. So the response store also keeps each turn's tool results. A replayed
   turn reuses them instead of re-running its tools, whose effects already happened before
   the crash. Tools still in flight at the crash run again.
9. **Strict schemas go through the SDK's `transform_schema` (amends ADR-008 §3).** Strict
   mode rejects numeric and length constraints. They move into the field description,
   where the model still reads them, and pydantic still enforces them client-side. Open
   `dict` fields are refused before the transform, because it would silently narrow them
   to `{}`.

## What the local sandbox is not

`LocalSandbox` confines paths and scrubs the environment. **It is not a network or syscall
boundary.** Repository code (tests, `conftest.py`, build hooks) runs with the user's
privileges. Steering HTTP clients to a dead proxy and setting package tools offline stops
well-behaved clients only. It is meant for trusted repositories. Untrusted ones need
`ContainerSandbox` (M10: `--network none`, read-only base, dropped capabilities).

## Verification

- Unit tests cover every module.
- **Fuzzed confinement** (`tests/property/test_path_confinement.py`). Hostile paths must
  agree with a reference resolver, never read from or write to the outside, and never let
  the Coder change a Tester file. Every known escape route is always tried as an explicit
  example. Each property was shown to fail when the defence it guards was removed.
- **Symlink race** (`tests/chaos/test_symlink_race.py`). `renameat2(RENAME_EXCHANGE)`
  flips a directory and an outside symlink roughly 100,000 times per run, while tools
  create and read through it. Several hundred calls per run resolve the real directory and
  meet the symlink at open time. All are refused, and the outside is never touched.
- **I-12 and I-13** (`tests/integration/test_capabilities.py`) run through the real gateway
  and tool layer with a scripted model.

## Consequences

- Tool output must be deterministic where it can be (sorted search results), and recorded
  where it cannot.
- POSIX only. Windows would need a different descriptor walk.
- `run` has four commands. A repository that needs others gets them added to the allowlist
  with typed arguments, never as a raw command line.

## Rejected

- **Check-then-open by name.** It loses the race above several hundred times a run.
- **Refusing all symlinks.** Repositories use in-tree links legitimately. Resolving once
  and walking without following keeps them working without the race.
- **A pure-Python search.** Python's `re` backtracks, so a model-supplied pattern could
  hang the run.
- **`preexec_fn` for rlimits.** It risks a deadlock between `fork` and `exec` when other
  threads hold locks.
- **`unshare --net` for local network isolation.** It needs user namespaces, behaves
  differently across hosts, and gives a false sense of a boundary. The container sandbox
  is the boundary.
