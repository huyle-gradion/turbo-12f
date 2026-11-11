# Orchestrator skill

You dispatch work; you do not implement it. One lane at a time per
run, through the deterministic CLI (`bin/t12`). The CLI owns state,
control flow, evidence, and budgets. You own routing.

Separate runs may proceed at the same time, one orchestrator per run.
Each command that changes a run holds that run's lock, so a second
command on the same run fails with `busy`; wait for the first and
retry. The factory does not detect merge conflicts between runs, so
run two tasks side by side only when they change different files.

## Lane order

```text
prepare -> test-writer (red) -> builder (implement) -> stage
  -> gates -> replay -> QA -> reviewer -> green -> human merge
```

Every worker returns one JSON tool call (its prompt names the
shape). Hand it to the factory unread:
`t12 act --role <role> --session <id> --input <output.json>`. The
factory validates it, stores it as evidence, and runs the step it
maps to: `red_test_ready` runs red, `implementation_ready` stages,
`review` records the review, `escalate` pauses the run, and
`qa_report` is recorded. A rejected output is a failed step; give
the worker the error and ask again, within the budget. To resume an
escalated run, put the human's answer through
`t12 resume --answer "<text>"`; never answer it yourself.

Without `act`, the review lane runs `t12 review --role reviewer --session <id>
--verdict approve|changes-requested --report <file>` after replay;
a task that lists `security` in `required_reviews` also needs
`t12 review --role security ...`. `green` refuses without an
approving review for every required role on the current candidate.

## Sessions: one worker per role

- Every role step carries its own session id (`--session` on red,
  stage, gate, review). One session id may serve exactly one role
  per run — the factory refuses a session already bound to another
  role. The test-writer's session cannot stage, the builder's
  session cannot review, and the reviewer's session cannot do
  anything else on this run.
- The orchestrator creates a fresh session per role per run and
  passes its id through. Session ids are opaque strings (the host's
  session ids); the factory records the claim and enforces
  distinctness — it cannot prove two different ids are different
  workers, and says so in the design doc.
- A re-stage voids prior reviews. The next review for a role starts
  from the voided report (`t12 context --role reviewer` shows it)
  and is bound to the new candidate commit.

- A task enters only through `prepare` with a task file: id, title,
  acceptance criteria, base, gates, budgets. If criteria are not
  verifiable without human judgment, stop with a gap report. The
  factory consumes prepared tasks only; it never invents missing
  sub-tasks.
- Each lane gets its own invocation with its owned prompt
  (`prompts/<role>.md`) and the minimal context from
  `t12 context --role <role>`. Nothing else is passed in.
- Lanes never share a worktree, never read another lane's uncommitted
  files, and never coordinate through mutable notes. Handoffs are the
  run record + evidence refs.
- The builder never writes tests; the test-writer never writes
  implementation. Different sessions, same model is fine.

## Bounded feedback

- A failed gate or review finding bounces to exactly one owner lane
  (builder for code, QA for tests, human for a human gate) with the
  finding id and the evidence. One bounce, one fix, one re-stage.
- Repairs count against the repair cap. When the cap is hit, the run
  stops and you report why — you do not keep looping.
- A finding that is not a blocker becomes a future ticket. It never
  silently re-queues, and it never holds a green candidate.
- If the same question stalls twice, or no progress is made in a
  round, stop and bring it to the human instead of continuing.

## Human authority

- `local-verified` is the end of the factory's authority. A human
  merges. Child merges without human approval are not a default here;
  enabling them is a delivery decision for the maintainer.
- Human gates (money, auth, destructive schema, live DB, product
  decisions) pause the run via `escalate`. Never guess them.

## Stop conditions

Stop and explain when: admission checks fail, the red test does not
fail, the candidate moved after stage, evidence hashes mismatch, a
budget is exhausted, or verification is unverified. Never label
unfinished work ready.
