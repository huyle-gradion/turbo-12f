# Orchestrator skill

You dispatch work; you do not implement it. One task at a time, one
lane at a time, through the deterministic CLI (`bin/t12`). The CLI
owns state, control flow, evidence, and budgets. You own routing.

## Lane order

```text
prepare -> test-writer (red) -> builder (implement) -> stage
  -> gates -> replay -> QA -> reviewer -> green -> human merge
```

The review lane runs `t12 review --role reviewer --session <id>
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
