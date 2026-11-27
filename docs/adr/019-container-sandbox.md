# ADR-019: A container backend for repository commands, for repositories you do not trust

- **Status:** Accepted (2026-11-26)
- **Refines:** ADR-009 (the tool layer and the local sandbox), ADR-015 §4 (isolation)

## Context

The local sandbox confines paths, scrubs the environment, bounds every command, and takes
the network away where the kernel allows (`unshare -rn`). It is not a syscall boundary:
the repository's code (its tests, its conftest, a mypy plugin) runs as the user, and can
read and write anything they can. ADR-009 said so, and left untrusted repositories to a
container sandbox that did not exist.

## Decision

1. **A second backend, chosen in settings.** `[sandbox].backend` is `local` (the default:
   nothing changes) or `container`. `make_sandbox` builds the one named. The run context
   uses it for the agents' `run` tool and for every test gate. The admission and
   validation steps keep the local one: they run git on the user's own repository.
2. **One fresh container per command**, with `docker` or `podman`
   (`[sandbox.container].runtime`):
   - the worktree bind-mounted at its own path, writable, and the working directory;
   - `--read-only` for everything else, except a private `/tmp` (also `$HOME`) and the
     directories of absolute output paths the harness passes (a JUnit report in the run
     directory). Paths the model passes are always relative to the worktree, so it cannot
     name another directory;
   - `--network none`;
   - a non-root user: yours, so files it writes stay yours, or `nobody` if you are root
     (`--userns keep-id` with rootless podman); every capability dropped; no new
     privileges;
   - `--cpus`, `--memory` (and as much swap: none), `--pids-limit`, and `[sandbox]`'s
     timeout. When the time is up, or the run is cancelled, the container is removed by
     name: killing the client alone would leave it running.
3. **The image holds the toolchain.** `[sandbox.container].image` must contain the
   repository's interpreter, its dependencies, and pytest, ruff and mypy. Nothing is
   installed in the container, which has no network to do it with. Commands are built with
   `[sandbox.container].launcher` (default `python -m`) in place of `[sandbox].launcher`.
   The backend without an image is refused when the settings load.
4. **It fails closed.** With `backend = "container"`, a missing runtime, or one that cannot
   run containers (no daemon), stops a run before it starts: `msa run` and `msa check`
   exit with the reason, and the service records it as the run's driver error. A command
   that finds the runtime gone mid-run is refused, not run locally.
5. **What stays on this machine:**
   - searching (`rg`), a preference's regex check (`rg` too) and diffing (`git`): they
     read the worktree, and run none of its code. The image needs none of them. ripgrep
     follows a symlink it is named, so the regex check confines its paths again just
     before it runs: a link that code in a container planted cannot point it out of the
     worktree;
   - a harness tool that needs the network, the dependency audit: it reads the
     repository's dependency files, and runs none of its code.
6. **Network isolation is the container's.** The gates' `unshare -rn` wrapper is empty in a
   container (`network_wrapper` returns `()`): it is isolated, and the integration report
   says "without network (in a container with none)".

## Consequences

- A hostile repository's tests can write only its own worktree, and reach no host. They
  cannot exhaust the machine's memory or processes. Whether they can escape the container
  is the runtime's boundary, not this code's.
- A container per command costs its start-up, typically under a second. Builds are
  unchanged.
- Someone has to build an image with the repository's dependencies, and keep it current.
- Tests check the runtime's exact arguments without a runtime, and drive the sandbox with a
  fake `docker` (timeout, removal). Real containers prove the worktree is writable and
  nothing else, that there is no network, and the memory limit. Those skip where Docker
  cannot run, and CI (`MSA_REQUIRE_CONTAINER=1`) fails rather than skip them.
- End to end, CI runs the definition-of-done run (ADR-015) in containers as well, from an
  image of Python, pytest and ruff alone (`tests/fixtures/images/demo`, `MSA_DEMO_IMAGE`). It
  must reach the same verdict at every gate as the local run. A `docker` that logs its calls
  shows that every repository command went through `docker run --network none`, that only
  the image's interpreter ran there, and that every container was removed, across the crash
  and the resume. That run found a preference's regex check sent into the container, where
  an image without ripgrep failed it; it runs on this machine now, as point 5 says.

## Rejected

- **One long-lived container per run:** fewer start-ups, but state left by one command
  (a background process, a file in `/tmp`) would reach the next, and a crash would leave
  it running.
- **Installing dependencies in the container at run time:** it needs the network the
  container must not have, or a second, networked container running the repository's
  install hooks.
- **Falling back to the local backend** when there is no runtime: the setting exists
  because the repository is not trusted to run locally.
