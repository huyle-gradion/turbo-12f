# Adapters

Adapters are separate programs that run outside the factory core.
They hold the host's credentials and talk to the network. The core
never imports these scripts.

- **Intake** adapters turn issues into task files (see "Intake
  adapters" below).
- **Delivery** adapters write a JSON report that `t12 deliver`
  records as evidence (the rest of this file).

## GitHub — create a pull request (never merge)

```bash
export GITHUB_TOKEN=...
python3 adapters/github.py create-pr --home <state-home> --task-id <id> \
  --out delivery.json [--repo owner/name] [--title T] [--body B]
bin/t12 deliver --home <state-home> --task-id <id> \
  --adapter github --report delivery.json
```

- The branch must already be pushed; the adapter only reads and
  creates the PR, then reads it back.
- The repo comes from `--repo`, the task's `github_repo` field,
  or the origin remote.
- If a PR already exists for the branch with the reviewed head,
  the outcome is `confirmed` and no duplicate is created. If its
  head is a different commit, `rejected`.
- When the run has a `source` (the task came from intake), the
  default body ends its summary with `Closes #N` if the source is an
  issue of the same repo as the PR, and always with a `Source:` link
  to the issue. An issue in another repo or tracker gets only the
  link, so merging never closes it. The task's title, acceptance
  criteria and manifest follow inside code blocks, with a WORD JOINER
  after each `#` that precedes a digit: an issue author wrote that
  text, so it can neither close an issue nor hide the rest of the
  body. The default PR title breaks issue references the same way.
  An explicit `--body` is used as given.

## GitLab — create a merge request (never merge)

```bash
export GITLAB_TOKEN=...
export GITLAB_URL=https://gitlab.example.com   # default https://gitlab.com
python3 adapters/gitlab.py create-mr --home <state-home> --task-id <id> \
  --out delivery.json [--project group/name] [--title T] [--body B]
bin/t12 deliver --home <state-home> --task-id <id> \
  --adapter gitlab --report delivery.json
```

- Uses the REST v4 API at `$GITLAB_URL/api/v4`. The token is sent
  as a bearer token.
- The project comes from `--project`, the task's `gitlab_project`
  field, or the origin remote (only when its host is the
  `GITLAB_URL` host). Nested groups work: `group/sub/name`.
- The branch must already be pushed. The adapter finds an open MR
  for the branch, creates one if none, then reads it back and
  compares its `sha` to the reviewed head. An existing MR with the
  reviewed head is `confirmed` with no duplicate; with another
  head, `rejected`.

## Bitbucket Cloud — create a pull request (never merge)

```bash
export BITBUCKET_TOKEN=...
export BITBUCKET_URL=https://api.bitbucket.org/2.0   # the default
python3 adapters/bitbucket.py create-pr --home <state-home> --task-id <id> \
  --out delivery.json [--repo workspace/slug] [--title T] [--body B]
bin/t12 deliver --home <state-home> --task-id <id> \
  --adapter bitbucket --report delivery.json
```

- The token is sent as a bearer token.
- The repo comes from `--repo`, the task's `bitbucket_repo` field,
  or the origin remote (only on `bitbucket.org`).
- Same flow as GitLab: find an open PR for the branch, create one
  if none, read back `source.commit.hash`. Bitbucket may return an
  abbreviated hash, so it is compared as a prefix of the full
  reviewed SHA. A hash shorter than 12 characters cannot be
  compared, so the outcome is `uncertain`, never `confirmed`.

## Asana — mark a task complete

```bash
export ASANA_TOKEN=...
python3 adapters/asana.py complete --home <state-home> --task-id <id> \
  --out delivery.json [--task <asana-gid>]
bin/t12 deliver --home <state-home> --task-id <id> \
  --adapter asana --report delivery.json
```

- The Asana task GID comes from `--task` or the task's
  `asana_task_gid` field.
- The adapter comments with the manifest summary + candidate SHA,
  marks the task complete, and reads it back. An already-complete
  task is `confirmed` with no duplicate comment.

## Intake adapters

```bash
export GITHUB_TOKEN=...          # or GITLAB_TOKEN and GITLAB_URL
python3 adapters/github-issues.py intake --out-dir <queue dir> \
  [--repo-config repo/.t12/project.json | --trunk-repo repo --trunk main] \
  [--repo owner/name] \
  [--label factory] [--home <state-home>] [--api-base https://api.github.com]
python3 adapters/gitlab-issues.py intake --out-dir <queue dir> \
  [--repo-config ...] [--repo group/project] [--label factory] [--home ...]
bin/t12-queue --home <state-home> --queue <queue dir> --max 3
```

Each writes one `<id>.json` per usable issue into the directory
`bin/t12-queue` reads, then prints one line per issue:
`wrote`, `unchanged`, `skipped` (with the reason), or `has-run`, and
one `withdrawn` line per task file it deletes.
`github-issues` makes ids `gh-<number>`, `gitlab-issues` `gl-<iid>`.
`GITLAB_URL` defaults to `https://gitlab.com`; the GitLab `--repo` is
the project path, subgroups included.

`--repo-config` names a profile file; its `intake` key
(`{"adapter", "repo", "label"}`) supplies `--repo` and `--label`, and
the adapter refuses a profile that names a different intake adapter.
Point it at a checkout of trunk: `prepare` reads the profile from
trunk, intake reads the file you give it. Or give
`--trunk-repo PATH --trunk BRANCH` instead: intake then reads the
`.t12/project.json` committed at the tip of BRANCH in the repository at
PATH, ignoring uncommitted edits, and refuses a missing branch or
profile. The two forms cannot be combined. A label is required, from
either place: anyone can file an issue, and the label is what a
maintainer applies to say the factory may work on it. Only issues
that carry it are read, open ones only, and pull requests are
ignored.

**The issue format.** The body must have a heading named
"Acceptance criteria" (any level, case-insensitive, optionally
followed by a note such as "(Testable)") followed by a `- ` or `* `
list, or a `- [ ]` checklist, until the next heading. Each item is one
criterion. An issue whose body contains `<!--` anywhere is skipped:
the forge hides comment text from the maintainer who applied the
label, and forges disagree on what an odd comment or fence hides, so
intake does not guess. Every other line is read, code fences
included; visible text in a code block can only narrow a task.
An issue without such a list is skipped and the reason is printed;
intake never invents criteria. Two optional lines, anywhere, and only
as `Name: value`:

```text
Touches: src/auth, tests/auth
Depends on: #12, gh-14
```

`Touches` becomes the task's `touches` (relative paths, no spaces).
`Depends on` becomes `depends_on`; `#N` means `<prefix>-N` of the
same adapter. A malformed value skips the issue, because dropping it
would widen the task.

**What the task file holds.** Only `id`, `title`,
`acceptance_criteria`, `source` (`{"adapter", "ref", "url"}`, ref is
`owner/name#number`), and the two optional fields above. The adapter
builds it from that list and never copies a key out of an issue, so
`gates`, `required_reviews`, `sandbox_*`, `verify_deps`, budgets,
`roles` and paths cannot arrive through an issue; they come from the
profile on trunk. The issue text is still read by workers as the
task's spec, so who may apply the label is the trust decision.

**Re-running.** A file whose content is unchanged is not rewritten.
An edited issue rewrites its file, unless the task already has a run
in `--home`, which is then left alone. Without `--home` intake cannot
see runs, so pass it whenever a state home exists. A file in the
queue directory is overwritten only if its `source` names the same
adapter and the same `owner/name#number`; ids repeat across repos, so
`gh-1` from another repo is skipped with the reason printed.

**Withdrawing.** After a complete listing, intake deletes each task
file in the queue directory whose `source` names this adapter and
this repo but whose issue was not listed: a maintainer removed the
label, or the issue was closed. It prints `withdrawn <id>: <path>`.
A task with a run in `--home` is left alone. A failed listing
deletes nothing.

**Limits.** The issue author can still edit the body after a
maintainer applies the label, and the next intake run picks up the
edit, unless the task already has a run. Remove the label from an
issue whose author you do not trust to leave it alone. A write error
partway through a run (a full disk, a permission error) can leave
some task files written and others not; run intake again once the
error is fixed.

**Failure.** Intake writes no outcome report, so a failed run must
not look complete. It lists every page before it writes. A 403, 5xx or
transport error on the list, on any page, writes nothing, prints
`error: ... nothing written` to stderr and exits 1. Exit 2 means it
could not run (missing token, no label, unreadable profile).

## Runner — drive runs with no one in the inner loop

```bash
python3 adapters/runner.py --home <state-home> --queue <queue dir> --max 2 \
  --model-cmd "claude -p" [--notify-cmd "notify-send t12"]
```

Each pass admits tasks through `bin/t12-queue`, then takes one step
per run from `t12 board --json`. A step that leaves a run where it
was (a refused `act`, a failed replay, a failed model call) is not
retried in the same invocation. The runner exits when a pass moves no
run, so run it again from cron or when a task file lands in the queue.

- `replay`, `green` and `qa: gate N` run as `t12` commands. No model
  is called.
- Every other role step is a worker call. `t12 context` builds the
  prompt, the runner pipes it to `--model-cmd` in the run's worktree
  with `T12_TASK_ID`, `T12_ROLE`, `T12_SESSION` and `T12_WORKTREE`
  set, and passes the tool call in its reply to `t12 act`. The command
  must be allowed to edit the worktree and run `git commit` without a
  prompt. Each call gets a session id never used before.
- The runner takes the tool call out of the reply one of two ways, and
  `act` records which as `parsed_by`, with the whole reply as
  `worker-raw-N.txt` evidence when it differs from the call:
  - `schema`: for `claude` and `codex exec`, the runner adds the
    role's JSON Schema (`t12 tool-schema`) to the command, and the CLI
    holds the model to it while it writes. If the CLI exits with an
    error in that mode, the runner calls it once more without the
    schema.
  - `extracted`: any other reply, or one with no structured result,
    is searched for exactly one tool call, bare on its own line or in
    a fence. A call printed twice counts once. Two calls that differ,
    or none, pass the reply on unchanged as `raw`, and `act` rejects
    it as before.

  Other CLIs (`agy`, `gemini`) get no schema yet: `agy -p` takes the
  prompt as an argument, not on stdin.
- When `act` rejects the output, the runner sends the error back to
  the same session once, if the budget still has an operation left.
  A failed model call records nothing, and the next runner tries
  again.
- `human: ...`, `stopped: ...` and a spent budget are never acted on.
  After the last pass the runner prints `waiting <id>: <reason>` for
  each and for each run a step did not move. It runs `--notify-cmd`
  with that line as its last argument. An answer goes in through
  `t12 resume --answer`. A merge is a human's.
- The runner keeps no state. Kill it at any point and start it again:
  it carries on from the run records. A second runner on the same
  state home is refused.

The worker runs outside `bin/t12-cage`, as a hand-driven worker does
today, so the builder commits its own change.

## Report schema

```json
{"adapter": "github", "task_id": "demo-1", "head": "<40-hex sha>",
 "outcome": "confirmed | rejected | denied | uncertain",
 "ref": "https://.../pr/7", "detail": "...", "checked_at": 1762...}
```

`head` must equal the run's reviewed candidate commit, or
`t12 deliver` refuses it.

## Outcomes

| Outcome | Meaning |
|---|---|
| `confirmed` | Remote state read back and matching. Delivery is done. |
| `rejected` | Remote definitively said no, or remote state definitively does not match. |
| `denied` | The mutation was refused for permission/policy (403 on the create/complete call). |
| `uncertain` | State unknown (timeout, 5xx, a read that failed). Nothing may be assumed. |

Rules: a failed READ is always `uncertain` — a 401/403 or 5xx on
a read is not a denial and not a rejection. A failed MUTATION is
`denied` on 403, `rejected` on 404/422 and other definite 4xx,
`uncertain` on 5xx/timeout.

Forge-specific readings of those rules:

- A 404 on the branch is `rejected` only when the project or repo
  itself reads back. Forges also answer 404 for a project the token
  cannot see, so otherwise it is `uncertain`.
- An existing change request is `confirmed` only when it has the
  reviewed head and targets the run's base branch. The same head on
  another target is `rejected`.
- Bitbucket pull requests report an abbreviated hash, so the Bitbucket
  adapter also requires the branch's full head to equal the reviewed
  commit.

## The uncertain block

An uncertain outcome blocks further delivery for that adapter
until a read-back report is recorded with `--resolves`:

```bash
python3 adapters/github.py create-pr --home <state-home> --task-id <id> --out delivery2.json
bin/t12 deliver --home <state-home> --task-id <id> \
  --adapter github --report delivery2.json --resolves
```

Never retry the action blind after an uncertain outcome — the
first attempt may have landed. The block is per adapter.

## Exit codes

An adapter exits 0 when it wrote a report, whatever the outcome.
Non-zero means the adapter itself could not run (missing token,
missing run record, bad arguments) and no report exists.
