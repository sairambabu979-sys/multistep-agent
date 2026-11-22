# ADR-014: Review, negotiation and arbitration: scope by lines, assent by the other side, the ladder before any person

- **Status:** Accepted (2026-11-22)
- **Refines:** PLAN §4 (the negotiation protocol), ADR-004 (executable arbitration),
  ADR-011 (the stand-ins), ADR-012 (TESTING)

## Context

M8 replaces the Reviewer stand-in with a Reviewer, a negotiation between it and the
Coder, and the arbitration ladder of ADR-004. PLAN §4 names the three ways two models
negotiating fail quietly: the author accepts everything, the reviewer approves everything,
or findings keep arriving. The protocol has to make each of them structurally hard.

Three gaps in what existed had to close first:

- **The ladder was skipped under the default setting.** With `escalation = human` (D4),
  the core sent a deadlock straight to AWAITING_HUMAN, so no repro test, preference or
  style rule ever ran before a person was asked.
- **A person's answer would end the run.** The core fails any event after the deadline,
  and the deadline is 45 minutes from the start. A person answering later is the normal
  case.
- **There was no way to answer at all.** The plan gate and the ruling had no executor,
  so a live run that reached either failed.

## Decision

1. **Findings are admitted by the lines they point at.**
   - Every finding names a file and a line range. The harness admits it only if the range
     touches a line the change added or modified, from a `git diff -U0` of base and head.
   - On the review after a revision (cycle ≥ 1), a new finding must touch a line *that
     revision* changed. The exception is `reopens: <id>`, which names an earlier finding
     whose fix was agreed or upheld and is still missing. This keeps findings from
     arriving on code nobody touched, while an unfixed agreement can still be called out.
   - A `preference` finding must name a preference in force.
   - A blocking finding on the Tester's paths is admitted as `minor`: the author cannot
     change those files, so it is reported, not negotiated.
   - Ids are `c<cycle>-<n>`. Every finding, admitted or not, is kept with the reason in
     `findings.jsonl`, and the report lists it.
2. **A round closes a finding only by an accept or the other side's assent.**
   - The Coder answers as the author, with read-only tools. Each move needs a rationale.
     A rebut needs evidence, and a counter carries its fix as evidence: without them, the
     move does not count.
   - The Reviewer replies only to contested moves, and only sees the artifacts and the
     author's moves, never its reasoning.
   - accept → accepted. rebut + withdraw → withdrawn. counter + amend → accepted with
     the countered fix. counter + withdraw → withdrawn. defer + withdraw → deferred.
     Everything else, including no move and no reply, leaves the finding open.
   - **Deferral needs the reviewer's assent.** PLAN §4.2 lists deferral as closing a
     finding; letting the author do it alone would let it defer every finding away.
   - Several moves on a finding: the first counts. Moves on findings that are not open
     are ignored and noted. The core's rule that a round must shrink the open set, or
     escalate, is unchanged.
3. **The ladder always runs before anyone rules (a core change).**
   - Escalation always issues `RequestArbitration`, with `arbiter` true or false.
   - The rungs, per finding:
     - **correctness and security: a repro test.** The Tester writes
       `tests/repro/test_<run>_<finding>.py` in a scratch worktree at the head tree, and
       the harness runs it. A failed assertion upholds the finding; passing everywhere
       dismisses it. Anything else is inconclusive and the finding goes up a rung, with
       the reason: no test written, no test with a live check, a skip or xfail mark, a
       module that does not import, or an error.
     - **preference: the preference.** A hard one upholds, soft or info defers it as a
       follow-up, and one that does not apply to the file dismisses it.
     - **uncovered style: dismissed.** Taste never blocks.
   - With `arbiter`, the Arbiter role rules on the rest. It runs at `max` effort, with no
     tools, and must rule on every finding or the effect fails. Without `arbiter`, the
     ruling may be partial, and the core asks a person about what is left and nothing
     else.
   - A partial ruling that already demands a change when no revision is left fails fast,
     as a round does (the model checker's EF DONE rule).
4. **Upheld repro tests join the tree; repro tests are judged on the head.**
   - Before the Coder's revision for a finding a repro test upheld, the harness writes the
     test into the build worktree. The path is the Tester's, so the Coder can make the
     test pass but cannot change it.
   - TESTING exempts repro modules from fail-before. They already failed on the head they
     disputed, which is their evidence, and a regression's test passes on the base. They
     must still pass after the revision.
   - A dismissed repro test is stored and reported but not added to the patch. Without a
     revision there is no tree change to carry it.
5. **People answer from outside; the run parks, and its clock stops (a core change).**
   - The executor for a plan gate or a ruling writes `question.md` to the run directory
     and raises `Parked`. Once every pending effect is parked, the interpreter returns
     the run live and resumable. `msa approve` (`--reject`, `--abort`) and `msa rule`
     (`F=upheld|dismissed|deferred`, `--abort`) journal the answer through the core,
     which validates it, and resume the run unless `--no-resume` is given.
   - The core records `waiting_since` while a human effect is pending. Each answer moves
     the deadline by its wait before the deadline guard runs. Invariant:
     `deadline == started_at + wall_clock + paused`, and one person is asked at a time.
     `SystemClock` has no wait budget while a person is asked.
6. **The report shows the negotiation, and flags degenerate ones.**
   - It shows every finding with its outcome and what decided it, lists deferred findings
     as follow-ups, and gives the author's accept rate and the reviewer's withdraw rate.
   - PLAN §4.2's thresholds (accept > 95%, withdraw > 80%) flag a run only from five moves
     on, because one accepted finding is not a pattern. The M9 eval applies them across
     runs.
7. **Arm B stays available.** `[review].enabled = false` restores the pass-through
   stand-in, named in the report, for the ablation (PLAN §11). M6 and M7's scenarios run
   that way.

Rounds with two invocations (author, then reviewer), and arbitration with several (a repro
test per finding, the Arbiter), share one effect's reservation (`Purse`): each gets what
the earlier ones left, and a failure carries what the effect had already spent.

## Verification

- `tests/integration/test_m8_runs.py`, all against the real executors, sandbox and git,
  with only the model scripted:
  - **I-05 to I-10.** I-08 parks, is answered through the CLI, resumes, and shows the
    paused time on the deadline. I-10 shows the gateway refusing a turn the reservation
    cannot fund, with the ledger within its limit.
  - **The cheap rungs and the Arbiter.** Preferences and style settle without a model,
    and the Arbiter is asked only about the rest.
  - **Repro tests that cannot decide.** All five reasons climb the ladder to a person.
  - **The plan gate through `msa approve`:** reject, replan, approve, done; and abort.
- `tests/unit/runtime/test_negotiation.py`: the scope and round rules, case by case and as
  properties. Silence never closes a finding, only assent closes a contested one, moves on
  other findings change nothing, and admission never widens scope or raises severity. A
  broken assent rule was shown to fail the property.
- `tests/unit/core`: the new escalation path, partial rulings, and the paused clock.
  Mutation testing of the core kills all 1,129 mutants (1,126 killed, 3 by timeout). It
  first found two misplaced pragmas and one missing test, which are fixed.

## Consequences

- No model has negotiated yet. Every prompt here is untested against the real API until
  the cassette is recorded and M9 runs the ablation. ADR-004's condition stands: if the
  negotiation does not earn its cost, it ships disabled.
- A round costs up to two invocations, and each correctness dispute that reaches the
  ladder costs a repro test. Undisputed findings cost nothing beyond the review.
- Journals of *simulated* runs that reached AWAITING_HUMAN under the old escalation path
  no longer replay. Live runs never could reach it before M8.
- Tasks whose real fix lives in the Tester's tests cannot be negotiated: findings there
  are reported only.
- The move-level rules live in `msa.runtime.negotiation`, not in the core, where PLAN §10
  placed them: they interpret agent answers, which are agent-layer types. The core keeps
  the structural rules: bounded rounds, strict progress, exhaustive rulings, and the
  finalization gate.

## Rejected

- **Letting the author defer alone.** It turns deferral into a way out of every finding.
- **Repro tests in the build worktree.** Writing one during arbitration would change the
  head tree mid-dispute, and invalidate the evidence the dispute is about.
- **Counting a person's wait against the wall clock.** It makes `escalation = human`, the
  default, fail on any answer slower than the run's remaining minutes.
- **Escalating straight to a person under `escalation = human`.** That skips the
  objective rungs ADR-004 puts first, and asks a person what a test could have answered.
- **Strict per-finding output schemas** (one required key per open finding). They would
  force completeness, but a missing move is already safe: it leaves the finding open.
