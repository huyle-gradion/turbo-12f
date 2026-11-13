# Writing an issue the factory can deliver

The factory turns each acceptance criterion into one test, then writes
code until the tests pass. It cannot ask the issue's author what they
meant. An issue is ready when a person who has only the repository and
the issue could name the first failing test for every criterion without
asking anyone.

Adding the `factory` label is your approval of the issue text. Check
the list below first.

## Ready checklist

1. **No open decisions.** The issue has no "Open questions" section, or
   the section is empty. Write each choice the builder would have asked
   about as a decision.
2. **Every criterion is observable inside the verification cage.** A
   criterion names an input, an action and a result that a local test can
   check with no network, no live model, no socket and no git remote.
   Put anything that needs a live system under "Manual checks", not under
   "Acceptance criteria".
3. **One behavior per criterion.** Write each as "given X, when Y, then
   Z". Avoid "works", "properly", "support" and "handle". Cover the happy
   path, a failure, and an edge input.
4. **Declare `Touches:` and `Depends on:`.** List the files the issue
   changes and the issues it follows. Without `Touches:` the issue runs
   alone.
5. **Point at code.** Name the file and function to change and an
   existing test to copy the style from.
6. **State what is out of scope.** The builder stops there.

## When to split an issue

Split it into sub-issues, each ready on its own and chained with
`Depends on:`, when any of these is true:

- It has more than 6 acceptance criteria.
- It changes more than one module's responsibility.
- It adds a public interface and its callers.
- You cannot judge a failed attempt from one test file.

Keep a parent issue for the story. It closes when its last sub-issue
merges.

## What intake checks

Intake (`adapters/common.py`) refuses an issue with no "Acceptance
criteria" heading and list, or with an HTML comment. It does not yet check
items 1, 2 and 4 or the 6-criteria cap. Those checks are the next step
and are not part of this document.

## Template

Use `.github/ISSUE_TEMPLATE/factory-task.md`.
