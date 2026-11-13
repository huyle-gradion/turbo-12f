# Writing an issue the factory can deliver

The factory turns each acceptance criterion into a failing test, then
writes code until the tests pass. It cannot ask the issue's author what
they meant. An issue is ready when a person who has only the repository
and the issue could name the first failing test for every criterion
without asking anyone.

Adding the `factory` label is your approval of the issue text. Write the
issue with the `factory-spec` skill: the agent drafts it and questions you
until no decision is left open, then runs `spec-check` before filing.

## Rules

`spec-check` in `adapters/common.py` applies one standard for every
forge adapter. Intake runs the same function, so an issue that fails it
never reaches the queue. Rules that code can check are refusals. Rules
that need judgement are advice.

### Required (refused when missing)

1. **Acceptance criteria.** A heading "Acceptance criteria" followed by a
   list of 1 to 10 items. More than 10 is refused: split the issue.
2. **`Touches:`** A line naming the files the issue changes, separated by
   commas. It may name files that do not exist yet; intake reports them
   and does not refuse.
3. **No open questions.** Intake refuses a non-empty "Open questions"
   section, and an `_Open question:` line whose value is not `none`.
   Record each choice the builder would ask about in a "Decisions"
   section. Leave out "Decisions" when there is nothing to record.
4. **No HTML comments.** The factory reads the raw text, so a comment
   hides nothing from it and confuses it.
5. **Dependencies that can land.** Intake refuses a dependency on an
   issue that does not exist, is closed without a merged change, or is in
   another repository.
6. **No label on a parent.** Intake refuses a labeled issue that has
   sub-issues. Only the sub-issues run.

### Warned (you decide)

- **7 to 10 acceptance criteria.** The `factory-spec` skill proposes a
  split and you choose. There is no measured threshold for this; the
  retro records the count of every delivered issue so the threshold can
  be set from our own results.

### Recommended (advice only)

- **Summary.** What is wrong or missing, in two or three sentences.
- **Pointers.** The file and function to change, and an existing test to
  copy the style from.
- **Out of scope.** What the builder must not change. It stops there.
- **Dev notes.** Background that helps but is not a requirement.

`gh-issue` fields that the factory does not use (FS impact, Platform,
Size, Score) are ignored.

## Writing a criterion

Write each criterion as "given X, when Y, then Z", one behavior each.
Avoid "works", "properly", "support" and "handle". Cover the main path, a
failure, and an edge input.

Every criterion must be checkable by a local test inside the
verification cage. The cage allows none of these:

- network access, including loopback sockets and local servers;
- git transport (`push`, `fetch`, `clone`), even to a local bare
  repository;
- a live model, an agent CLI, or its quota;
- the user's home directory or credentials.

A check that needs any of them (for example a real `agy` run) is not a
criterion. `factory-spec` files it as a separate follow-up issue without
the `factory` label, for a person to run after the change merges.

## Size and splitting

One issue is proven by one test file. When one test file cannot judge a
failed attempt, split the issue.

Split into a parent issue without the label and one labeled sub-issue per
part, each ready on its own. The parent records the story and closes when
its last sub-issue merges.

## Order between issues

The forge's own relations are the source of truth:

- **Parent and child:** native sub-issues (GitHub) or subtasks (Asana),
  available on every plan.
- **Blocked by:** native dependencies. GitHub has them on every plan.
  GitLab has them on Premium and Ultimate. Asana has them on paid plans.

Only where the forge or plan has no native "blocked by" (GitLab Free,
Asana's free plan) does intake read a `Depends on: #12, #15` line from the
issue body. Never use both on one forge: on GitHub a `Depends on:` line is
refused.

## Template

Use `.github/ISSUE_TEMPLATE/factory-task.md`.
