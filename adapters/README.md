# Delivery adapters

Adapters are separate programs that run outside the factory core.
They hold the host's credentials, talk to the network, and write a
JSON report. `t12 deliver` records that report as evidence. The
core never imports these scripts.

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
