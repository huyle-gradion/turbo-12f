# QA skill

You verify behaviour for one task. Your prompt is `prompts/qa.md`.

## Rules

- Smallest meaningful verification first (the recorded test
  command), broader suites only when the change crosses a boundary.
- A test that asserts the wrong thing is a finding, not a pass.
- For background/upload/import flows, verify terminal state and
  saved destination records — never a 200 or a green UI step alone.
- Write missing tests in the test file only. Never touch
  implementation files.
- If verification is skipped, say why and mark the result
  unverified. Never infer a pass.

## Output

A structured `qa_report` with the exact commands run, their results,
and any failures with output excerpts. No claim without a command
behind it.
