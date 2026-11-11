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
