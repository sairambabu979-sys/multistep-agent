# ADR-018: After approval, push the release and open its pull request, if asked

- **Status:** Accepted (2026-11-26)
- **Refines:** ADR-015 §4 (publishing after a person approves), ADR-007 (crash semantics)

## Context

An approved release was committed to a local branch `aegis/<run>`, and nothing more. The
person who approved then pushed it and opened the pull request from `pr.md` by hand. That
kept every credential out of the system. But it left the last step of the definition of
done, an auditable PR, to manual work, and nothing recorded whether it ever happened.

## Decision

1. **Opt-in, and chosen by the person who approves.** `[publish].mode` is `local` (the
   default: behavior is unchanged), `push`, or `pr`. `msa approve --publish` overrides it
   for one approval, and so does `{"publish": ...}` on `POST /api/runs/<run>/approve`.
   Naming a mode for a plan gate or a rejection is refused: only an approved release has
   anything to publish.
2. **The mode is journaled with the approval.** `ReleaseApproved.publish` carries it, and
   the core copies it to `PublishRelease.publish`. A change to `msa.toml` after the
   approval does not change what a resumed run does.
3. **The result is journaled with the publication.** `ReleasePublished.publication` (a
   `Publication`: mode, local branch, commit, remote, remote branch, PR URL, error) is
   folded into `RunState.publication`, so the API and the workbench show it, and it
   survives a crash with the rest of the journal. It is `None` in journals written before
   this record.
4. **The release is committed first, and failures are recorded, not raised.** The local
   commit is unchanged. A push or a pull request that fails is recorded as the
   publication's error, and the run still completes. The approved commit is safe on its
   branch, and the journal says what did not happen.
5. **Credentials stay where they are.**
   - The push is plain `git push`, with the user's git configuration, so their credential
     helper or SSH key authenticates it. Hooks are off, git never prompts, and the push is
     never forced: a remote branch holding something else stays as it is, and the push
     fails.
   - The pull request uses `gh` when it is installed and logged in, otherwise the REST API
     with the token in `$[publish].token_env` (default `GITHUB_TOKEN`).
   - The token travels in the Authorization header only. It is in no repr and no recorded
     message: credentials in URLs and the token's value are scrubbed from every error.
   - The token variable is read by the harness, never passed to a sandboxed command.
6. **Idempotent, so at-least-once is safe (ADR-007).** Run again after a crash:
   - the local commit is found and reused;
   - pushing the same commit changes nothing;
   - an open pull request for the branch is found and reused, not opened twice.
7. **The body is `pr.md`,** without its `# title` line (that is the pull request's title),
   cut to fit GitHub's size limit with a note that the commit message holds all of it.

## Consequences

- With `mode = "pr"`, an approval ends with a real pull request whose description is the
  evidence `pr.md` records, and the run's outcome names it.
- A failed push or pull request needs a person, who reads the error in the run view. There
  is no retry command: they push or open it by hand, as before.
- Tests use a bare repository as the remote, a recording `gh` script, and a REST server on
  the loopback interface. No test touches the network.

## Rejected

- **A token in the push URL** (`https://x-access-token:...@github.com/...`): the token
  would be in the process's arguments, and in any error git prints.
- **Failing the run when the push fails:** the release is approved and committed. Failing
  would hide the commit behind a failed run, and a retry would need a new approval.
- **A separate state and effect for publishing:** it adds a phase to the state machine for
  what is one idempotent effect, with nothing new to decide.
