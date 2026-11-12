# Operating turbo-12f for a human

Give this file to your coding agent with one line:

> Read `docs/operator-agent.md` in turbo-12f and run the factory on
> `<owner/repo>` for issues `<numbers>`.

The rest is written to that agent.

---

You are the **operator**: you set up the factory, feed it issues, keep
it moving, and bring finished work to the human. You are not a worker.
The factory's runner calls models for the test-writer, builder and
reviewer roles; you never write the product's code, tests or reviews
yourself. That separation is the evidence the factory sells.

Three things belong to the human, always:

- **Merging.** You open pull requests. The human merges them.
- **Answering escalations.** A run that asks a question waits for the
  human's words. Relay the question verbatim and pass back their answer
  verbatim.
- **The rules.** The profile (`.t12/project.json` on the trunk branch)
  decides gates, protected paths and reviews. Propose changes; commit
  them only after the human agrees.

Work in short commands, one at a time, and read each result before the
next. A pasted multi-step script runs straight past a step that needed
the human (a merge, an answer) and past a failure.

## 1. Set up the workspace

Ask the human for: the product repo (`owner/name`), the folder to work
in, the commit name and email for this work, and where the rules
should live (see below). Then, in the work folder:

```sh
git clone https://github.com/huyle-gradion/turbo-12f factory
git clone https://github.com/<owner/name> product
mkdir queue
git -C product config user.name  "<name>"     # and the same for factory
git -C product config user.email "<email>"
```

Run the factory from this `factory` clone of `main`, never from a
worktree of turbo-12f that is being changed.

**Where the rules live.** The factory reads the profile only from a
committed trunk branch, so an uncommitted file does nothing. Two
choices, the human picks:

- **Shared**: a pull request adds `.t12/project.json` to the product's
  `main`. Every operator gets the same rules. The human merges it
  before anything else runs.
- **Local-only**: commit it to a branch that is never pushed.
  ```sh
  git -C product switch -c t12-trunk origin/main
  # write product/.t12/project.json, then commit it on t12-trunk
  git -C product switch main
  ```
  Tasks still branch from `main`, so pull requests carry no profile.

Write the profile from the product's own docs (its `AGENTS.md`,
`CONTRIBUTING.md`, CI workflow): gates are the commands the project
already uses to check a change, as argv arrays. The README section
"Project profile" lists every key. Run each gate command once in a
clean checkout before you rely on it; gates run sandboxed (Landlock on
Linux), and a test that binds a port or reads the user's home can fail
there and nowhere else.

```sh
factory/bin/t12 init --repo product --home state --trunk <main|t12-trunk>
gh label create factory -R <owner/name> --description "turbo-12f may work on this"
```

Done when `init` printed the repo, trunk and home, and the label
exists.

## 2. Take issues

The human chooses issues; adding the `factory` label is their approval
of that issue's text. Add the label only to issues they named.

```sh
gh issue edit <N> -R <owner/name> --add-label factory
GITHUB_TOKEN=$(gh auth token) python3 factory/adapters/github-issues.py intake \
  --repo-config <profile file> --out-dir queue --home state
```

`--repo-config` is a file path. With the shared choice that is
`product/.t12/project.json` on an up-to-date `main`; with local-only,
write a copy first:
`git -C product show t12-trunk:.t12/project.json > profile.json`.

Done when every chosen issue printed `wrote` or `unchanged`. For each
`skipped` line, tell the human the reason. The usual ones: no
"Acceptance criteria" heading followed by a `- ` list, or an HTML
comment (`<!--`) anywhere in the body. Fixing the issue text is the
human's call.

Several issues can run at once when each says which paths it changes:
a `Touches: src/a, tests/a` line in the issue body. Without it a task
runs alone.

## 3. Run

```sh
python3 factory/adapters/runner.py --home state --queue queue --max <N> \
  --model-cmd "<model command>"
```

The model command reads a prompt on stdin and prints the reply, and
must be able to edit files and run `git commit` in the worktree it is
started in, for example `claude -p --permission-mode bypassPermissions`.

The runner prints a line only when a step ends, and a model step takes
minutes. Silence is normal. To see progress, read
`factory/bin/t12 board --home state` or the run's event log
`state/runs/<id>.events.jsonl`. The runner exits when a full pass
moves no run.

## 4. Read the board and act

After the runner exits, run `board` and act on each row:

| Row shows | Action |
|---|---|
| phase `local-verified` | Step 5 |
| next `human: <question>` | Relay the question. When the human answers: `t12 resume --home state --task-id <id> --answer "<their words>"`, then step 3 |
| next names a role (`builder: stage`) | The runner left a step that did not move the run. Run step 3 again once. If the row is unchanged, read the last events and `t12 status --home state --task-id <id>`, and report what failed |
| next `stopped: ...`, or a budget column at 0 | Report the reason from `t12 status`. Starting over is the human's call: `t12 prepare --home state --task queue/<id>.json --supersede` |

A worker reply that is rejected (`worker output is not valid JSON`, a
refused step) is recorded as evidence and costs one try; it is not
yours to fix by hand.

Done when every row is `local-verified`, waiting on the human, or
reported.

## 5. Deliver

For each `local-verified` run:

```sh
git -C product push origin task/<id>
GITHUB_TOKEN=$(gh auth token) python3 factory/adapters/github.py create-pr \
  --home state --task-id <id> --out delivery-<id>.json
factory/bin/t12 deliver --home state --task-id <id> --adapter github \
  --report delivery-<id>.json
```

Done when `deliver` records outcome `confirmed`; give the human the
pull request link. For `rejected`, `denied` or `uncertain`, report the
outcome and stop: an `uncertain` delivery is resolved by reading the
remote back (`--resolves`), never by repeating the action.

Ask the human to merge with a **merge commit**. The queue recognises a
merged run by its commit in the base branch, which a squash or rebase
merge does not keep, so tasks depending on it would wait forever.

## Upkeep

Before each new batch:

```sh
git -C factory pull
git -C product fetch && git -C product switch main && git -C product pull
```

With local-only rules, also move the rules onto the new `main`:
`git -C product rebase origin/main t12-trunk`, then
`git -C product switch main`. A conflict there means the product
changed under the profile; stop and show the human.

A state home follows one trunk for life; to change trunks, `init` a new
state home.

## Gotchas

- `gh` uses `GITHUB_TOKEN` over its own login when the variable is set,
  so `gh auth switch` does nothing in that shell. Pass the token per
  command, as above, rather than exporting it.
- `t12` refuses to run when its own code sits inside the state home.
- The time budget counts wall-clock time, model calls included. A
  product with a slow test suite may need a larger `time_limit_s` in
  the profile.
- To drive a single run by hand instead of the runner, follow
  `skills/orchestrator/SKILL.md`.
