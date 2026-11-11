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

## Done

You are done when the change is committed in the worktree and you
have reported the summary. Verification, review, and merge are other
lanes' jobs, not yours.
