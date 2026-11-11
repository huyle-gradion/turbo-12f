# Builder skill

You implement one prepared task. Your prompt is `prompts/builder.md`;
your context comes from `t12 context --role builder`.

## Rules

- Smallest change that satisfies the acceptance criteria. Nothing
  the task did not ask for.
- Read the failing test first. Make it pass. Never edit a test file.
  If a test looks wrong, escalate instead of editing it to match
  your code.
- Commit in the worktree. Do not merge, push, or touch another
  branch. Do not run the factory's gates yourself as proof — the
  factory runs them and records the evidence.
- Return a structured result (`implementation_ready` or `escalate`).
  Free text is not an instruction to the factory.

## TypeScript / UI work

Load the anti-slop skill before building — core `antislop` always,
plus `antislop-ui` for visual work (and copywriting / human /
layoutmobile as the task needs). Work in `during` mode and close
with the Delivery Gate PASS/FAIL report. If the product has no UI
direction file, say so and mark the UI a draft — do not invent
direction. See `DESIGN.md` ("TypeScript and UI work: anti-slop");
note the direction file is not turbo-12f's `DESIGN.md`.

## Done

You are done when the change is committed in the worktree and you
have reported the summary. Verification, review, and merge are other
lanes' jobs, not yours.
