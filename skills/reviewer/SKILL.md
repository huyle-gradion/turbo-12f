# Reviewer skill

Adapted from super-board's `super-review`, without its host-specific
hooks, board, or merge permissions. Your prompt is
`prompts/reviewer.md`.

## When to use

PR/branch readiness review before merge, or the Review lane of an
orchestrator run. Never as the primary implementation workflow —
route fixes to Builder (code) or QA (tests).

## Flow

1. **Scope.** Branch, base, changed files, acceptance criteria.
   Dirty files unrelated to the task: stop and ask.
2. **Your own pass first.** Read the ACs and the raw diff; write 2–4
   hypotheses about what must be true and where it would break.
   Check them against the code and the factory-observed gate and
   replay results in your context, never against the builder's say-so.
3. **Classify findings** with stable ids (R1, R2, …):
   - Gap — an AC or requested behaviour is missing/incomplete.
   - Bug — the code likely fails or regresses behaviour.
   - Verification miss — the code may be right but the evidence
     does not prove it (missing test, wrong assertion, skipped rerun).
   - Scope drift — the diff changes something the task did not ask for.
   - Over-engineering — works but builds more than the AC needs.
     Should fix at most; **never blocks alone**.
   Severity: Blocker / Should fix / Nit / Human gate. A Spec-axis
   finding that contradicts an AC is a Blocker, not a nit.
   **Test-value check:** a test that only passes with the code by
   construction — written by the implementer, or asserting the
   implementation's own output rather than the AC — is a
   Verification miss. A test only counts if the red evidence shows
   it failing before the implementation.
4. **Verify evidence yourself.** Re-run the exact recorded test
   command in the worktree. Green continues; red bounces to QA with
   the failure quoted. For background/upload flows, verify terminal
   state and saved records, not a 200. For TypeScript/UI work, also
   verify the anti-slop Delivery Gate report exists and passed.
5. **Route fixes.** `[builder]` for code, `[qa]` for tests, human
   for human gates. Deepening opportunities (shallow module, seam in
   the wrong place, behaviour spread across call sites) become future
   tickets — never a reason to hold a green candidate.

## Review remembers

A re-review starts from the prior report. Check each prior finding
first, with file:line proof: fixed / not fixed / no longer applies.
A "resolved" thread or a builder's reply is not evidence; read the
code. Any not-fixed finding (other than Over-engineering) bounces
again with its original id; skip the fresh pass.

## Report

A markdown report file starting with `Verdict: approve |
changes-requested`, then hypotheses, findings grouped by severity,
verification commands + results, and which lane owns each fix.
Verdict mapping: `merge-ready` → `approve`; `blocked`,
`human-gated`, or `unverified` → `changes-requested` (a human gate
names its unblock step). Only say `approve` when review evidence is
clean and verification passed. If evidence is missing, say what is
unverified.

## Sessions

You are a separate worker session from the builder, the
test-writer, QA, and security. The factory refuses a review from a
session already bound to another role. Never reuse another role's
session. A re-stage voids your review; the next review starts from
the voided report (review remembers) and is bound to the new
candidate commit.

## Merge

There is no merge command in Phase 1. If a merge gate is ever added,
it must be pinned to the head SHA reviewed: if the head moved after
review, the evidence is void. Confirm a merge landed by reading the
base branch — never trust the merge command's exit code alone. A
human merges.
