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
  link, so merging never closes it. An explicit `--body` is used as
  given.

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
  [--repo-config repo/.t12/project.json] [--repo owner/name] \
  [--label factory] [--home <state-home>] [--api-base https://api.github.com]
python3 adapters/gitlab-issues.py intake --out-dir <queue dir> \
  [--repo-config ...] [--repo group/project] [--label factory] [--home ...]
bin/t12-queue --home <state-home> --queue <queue dir> --max 3
```

Each writes one `<id>.json` per usable issue into the directory
`bin/t12-queue` reads, then prints one line per issue:
`wrote`, `unchanged`, `skipped` (with the reason), or `has-run`.
`github-issues` makes ids `gh-<number>`, `gitlab-issues` `gl-<iid>`.
`GITLAB_URL` defaults to `https://gitlab.com`; the GitLab `--repo` is
the project path, subgroups included.

`--repo-config` names a profile file; its `intake` key
(`{"adapter", "repo", "label"}`) supplies `--repo` and `--label`, and
the adapter refuses a profile that names a different intake adapter.
Point it at a checkout of trunk: `prepare` reads the profile from
trunk, intake reads the file you give it. A label is required, from
either place: anyone can file an issue, and the label is what a
maintainer applies to say the factory may work on it. Only issues
that carry it are read, open ones only, and pull requests are
ignored.

**The issue format.** The body must have a heading named
"Acceptance criteria" (any level, case-insensitive) followed by a
`- ` or `* ` list, or a `- [ ]` checklist, until the next heading.
Each item is one criterion. Anything inside a code fence is ignored.
An issue without such a list is skipped and the reason is printed;
intake never invents criteria. Two optional lines, anywhere outside
a code fence, and only as `Name: value`:

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
queue directory that intake did not write is never overwritten. Intake
never deletes files: remove the file of a closed issue yourself.

**Failure.** Intake writes no outcome report, so a failed run must
not look complete. It lists every page before it writes. A 403, 5xx or
transport error on the list, on any page, writes nothing, prints
`error: ... nothing written` to stderr and exits 1. Exit 2 means it
could not run (missing token, no label, unreadable profile).

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
