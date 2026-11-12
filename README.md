# turbo-12f

A local-only rebuild of the best of Turbo, strictly on the
12-Factor Agents manifesto (Dex Horthy, HumanLayer) and the
super-board reviewer workflow. It is an evidence-and-policy layer
around AI coding work: it does not make the agent smarter, it makes
agent work bounded, reproducible, inspectable, and hard to mark
"successful" without matching evidence.

This is **not** the GitHub `huyle-gradion/turbo` repo. It lives only
on this machine, has no GitHub/Asana integration yet, and never calls
a model itself — the model is invoked by the operator with the owned
prompts in `prompts/`, and this CLI is the deterministic shell around
it. See `DESIGN.md` for the full architecture and the factor-by-factor
map, and `CONTEXT.md` for truth boundaries.

Python standard library only. No network, no credentials, no model
calls in the core or the tests.

## Quick start

```sh
# 1. Point it at a product repo (state lives outside the repo)
bin/t12 init --repo /path/to/product --home /path/to/state-home

# 2. Admit a task (examples/task.json)
bin/t12 prepare --home /path/to/state-home --task examples/task.json

# 3. Ask the test-writer for a failing test, then record it
bin/t12 context --home /path/to/state-home --task-id demo-1 \
  --role test-writer --phase red
bin/t12 red --home /path/to/state-home --task-id demo-1 \
  --test-path tests/test_demo.py \
  --command "python3 -m pytest tests/test_demo.py -q" \
  --marker "FAILED"

# 4. Ask the builder to implement, then pin the candidate
bin/t12 context --home /path/to/state-home --task-id demo-1 \
  --role builder --phase implement
bin/t12 stage --home /path/to/state-home --task-id demo-1

# 5. Run the gates in order, replay the red test, review, certify
bin/t12 gate --home /path/to/state-home --task-id demo-1 --name unit
bin/t12 replay --home /path/to/state-home --task-id demo-1
bin/t12 review --home /path/to/state-home --task-id demo-1 --role reviewer \
  --session <reviewer-session> --verdict approve --report review.md
bin/t12 green --home /path/to/state-home --task-id demo-1
bin/t12 manifest --home /path/to/state-home --task-id demo-1
```

Each role step carries its own session id (`--session` on red,
stage, gate, review); one session serves exactly one role per run,
so the test-writer cannot stage and the builder cannot review. A
task lists the reviews green requires in `required_reviews`
(default `["reviewer"]`; add `"security"` when it matters).

## Delivery

The core never touches the network. Adapters do the remote work and
write a report; `t12 deliver` records it:

```bash
export GITHUB_TOKEN=...   # or ASANA_TOKEN for the Asana adapter
python3 adapters/github.py create-pr --home /path/to/state-home \
  --task-id demo-1 --out delivery.json
bin/t12 deliver --home /path/to/state-home --task-id demo-1 \
  --adapter github --report delivery.json
```

Outcomes: `confirmed` (remote read back and matching), `rejected`
(remote said no), `denied` (mutation refused for permission),
`uncertain` (state unknown). An uncertain outcome blocks further
delivery for that adapter until a read-back report is recorded
with `--resolves` — never retry the action blind. The adapter
never merges; a human merges the PR. See `adapters/README.md`.

`local-verified` means the configured evidence chain was observed and
is internally consistent. It is not acceptance and not permission to
merge — a human merges.

## Commands

| Command | What it does |
|---|---|
| `init` | Bind a repo + state home, create the directory layout |
| `prepare` | Admit a task: validate repo/base/branch/gates, create worktree + run record (`--supersede` archives a stopped run and starts fresh) |
| `context` | Print the owned prompt + minimal context for a role (Factor 2/3/9) |
| `red` | Run the test command, expect failure + marker, commit the test, record red evidence |
| `stage` | Pin the candidate commit; verify ancestry, clean tree, unchanged test blob |
| `gate` | Run one gate in order; failure counts against the repair cap |
| `replay` | Re-run the exact red command on the candidate; expect success |
| `review` | Record a reviewer/security review: `--role reviewer\|security --session S --verdict approve\|changes-requested --report FILE` |
| `green` | Re-hash and re-validate all evidence; requires replay + approving reviews; certify `local-verified` |
| `manifest` | Write the manifest (recorded as an event with its SHA-256) |
| `verify` | Audit the run: event chain, evidence hashes, exit codes, HEAD, manifest |
| `probe` | Run the safety probes; exit 0 only if every probe passes |
| `deliver` | Record an adapter's report as evidence: `--adapter github\|asana --report FILE [--resolves]` (requires `local-verified`, pinned to the reviewed head) |
| `board` | One row per run: phase, candidate, budget left, whether a command is running on it, and who acts next (`--json` for scripts) |
| `report` | Budget report across runs: usage vs budget, cap hits, calibration summary (`--json` for scripts) |
| `retro` | Lessons that recur across runs (gate failures and suite time per gate, rejected worker outputs, review finding classes, repeated escalations), each with a proposed check; changes nothing (`--min N`, `--json`) |
| `calibrate` | Recommend defaults from completed runs; `--apply` writes `limits.json` |
| `status` | Show phase, budgets, gates, candidate |
| `pause` / `resume` | Pause with a reason; resume re-validates from disk (`--answer TEXT` is required while an escalation is open, and records the human's reply) |
| `stop` | Stop a run explicitly with a reason (terminal; use `--supersede` to start over) |
| `escalate` | Record a human question as a tool call and pause (Factor 7) |
| `act` | Take a worker's JSON output: `--role R --session S --input FILE`. Validates it against `t12/tools.py`, stores it as evidence, and runs the step it maps to (Factor 4) |

## Task file

```json
{
  "id": "demo-1",
  "title": "Add a demo behaviour",
  "acceptance_criteria": ["one checkable line per criterion"],
  "base": "main",
  "time_limit_s": 3600,
  "op_limit": 100,
  "repair_cap": 3,
  "gates": [
    {"name": "unit", "command": ["python3", "-m", "pytest", "tests/", "-q"]}
  ]
}
```

Gate commands are argv arrays, never shell strings. The task file is
hashed at admission; `green` refuses if the recorded config changed.
Set `"require_probes": true` to hard-gate red/gate/replay on passing
safety probes (see `bin/t12 probe`).

Verification commands run sandboxed. By default the adapter is
auto-detected (Landlock on Linux, `sandbox-exec` on macOS, none
otherwise). A task may request `"sandbox_adapter": "docker"` (or the
operator sets `T12_SANDBOX_ADAPTER=docker`): verification then runs
as `docker run --rm --network none` with the worktree and
verify-home mounted and only the verification env passed in. The
image comes from `"sandbox_image"` (default `python:3.12-slim`, or
`T12_SANDBOX_IMAGE`) and must already be present locally —
`prepare` refuses when the Docker daemon does not answer or the
image is missing, so there are no implicit pulls mid-run.

Red, gates, and replay run in a fresh checkout of the commit under
test, so untracked and ignored files in the worktree are invisible to
them. A task that needs gitignored dependency folders (`.venv`,
`node_modules`) lists them in `"verify_deps": [".venv"]`: relative
paths that must stay untracked. Each is linked into the checkout and
is read-only under Landlock and Docker (not under adapter `none`),
and the observation records which deps applied.

Budgets (`time_limit_s`, `op_limit`, `repair_cap`) are optional in
the task file. When omitted, `prepare` uses `limits.json` in the
state home if `t12 calibrate --apply` has written one, else the
built-ins (3600s / 100 ops / 3 repairs). The run records which
source applied (`budget_source`).

## Project profile

A project keeps what its tasks share in `.t12/project.json`, so a task
file only says what is unique to it:

```json
{
  "version": 1,
  "name": "my-product",
  "gates": [{"name": "unit", "command": ["python3", "-m", "pytest", "-q"]}],
  "required_reviews": ["reviewer"],
  "sandbox_adapter": "docker",
  "sandbox_image": "my-product-verify:1",
  "verify_deps": [".venv"],
  "protected_paths": ["guards", "e2e"],
  "security_review_paths": ["src/auth"]
}
```

`prepare` reads the profile from the base commit, never the worktree,
and stores the merged task in the run. A candidate that edits the
profile changes nothing for its own run. Unknown keys are refused.

A task may tighten the profile but never loosen it, because task files
may come from an issue tracker:

| Key | When both the profile and the task set it |
|---|---|
| `gates` | The profile's gates run first, then the task's |
| `required_reviews` | Both lists apply |
| `require_probes` | On if either turns it on |
| `sandbox_adapter`, `sandbox_image` | Refused unless equal |
| `verify_deps`, budgets | The task wins |
| `protected_paths`, `security_review_paths` | Profile only; a task may not set them |

`stage` refuses a candidate that changes a protected path since the
base commit. A move out of a protected path counts as a change to it.

## Running several tasks at once

Separate runs share one state home and proceed in parallel. Each
command that changes a run holds that run's lock, so a second command
on the same run fails with `busy` instead of corrupting its log.
`t12 board` shows every run and who acts next.

Two optional task fields keep parallel runs from colliding at merge:

- `"touches": ["src/auth", "tests/auth"]` lists the paths the task
  may change. `stage` refuses a candidate that changes anything else
  since the base commit, including the test the test-writer added.
  A task without `touches` may change anything.
- `"depends_on": ["task-a"]` names tasks that must be merged first.

`bin/t12-queue --home <state> --queue <dir> --max N` makes one pass
over a directory of task files in file-name order. It prepares each
task whose dependencies are local-verified and merged into their
base, whose `touches` overlap no unmerged run, and that fits under N
active runs. Every other task stays queued with its reason. A task
without `touches` overlaps everything, so it runs alone. Start one
orchestrator per `started` line and run the pass again whenever a
run finishes or a merge lands; a task that already has a run is never
prepared twice.

The queue counts a run as merged when its candidate commit is in the
base branch's history, as after a merge commit or a fast-forward. A
squash or rebase merge creates new commits, so the queue never sees
that run as merged: tasks that depend on it, or overlap it, wait. Use
merge commits for factory branches.

Runs prepared before `touches` existed carry no `base_commit`, so
`stage` skips the `touches` and profile checks for them. Finish or
supersede them before relying on either. Runs prepared after `touches`
but before project profiles are refused at `stage`; supersede them.

## Improving the factory

`t12 retro` reads every run's event log and structured reviews and
lists what keeps recurring: a gate that fails in most runs, a worker
output rejected for the same reason, a review finding class, a
question escalated again and again. Each comes with a proposed check.
Prefer a check (a gate, a probe, a refusal in `stage`, a stricter
field in `tools.py`) over more prompt text: a worker can ignore a
prompt, not a check. Like `calibrate`, `retro` only proposes. Turn a
proposal into a task file and run it through the factory; a human
merges.

## Letting the factory work on itself

turbo-12f is a repo like any other, so a `retro` proposal can become
a task aimed at turbo-12f. Two rules keep that safe:

- **The candidate never checks itself.** Run the factory from an
  installed copy of `main` (or the `turbo-12f` image through
  `bin/t12-container`), never from a worktree. `t12` refuses to run
  when its own code sits inside the state home it is given.
- **Profile changes need a security review.** A product repo may list
  paths in `.t12/project.json` as `"security_review_paths"`. `stage`
  reads the profile from the run's base commit, not the candidate, and
  adds `security` to `required_reviews` when the candidate changes a
  listed path or anything under `.t12/`. turbo-12f lists its state
  machine, profile, sandbox, evidence and storage code, and its prompts.

The factory still stops at `local-verified`. A human merges.

## Running in a container

`bin/t12-container` takes the same arguments as `bin/t12` and runs
the whole factory inside `docker run`:

```sh
docker build -t turbo-12f container/
bin/t12-container init --repo /path/to/product --home /path/to/state-home
bin/t12-container prepare --home /path/to/state-home --task examples/task.json
```

The container runs as your uid and gid with no network, a read-only
root filesystem, and no capabilities. It can see only these paths:

- this checkout, read-only
- the current directory
- every path argument (a file argument mounts its parent directory)
- the product repo recorded in the state home

Each path is mounted at its host location, so a state home works
from the host and from the container in either order. Your
`git config user.name` and `user.email` are passed in as
`GIT_AUTHOR_*` and `GIT_COMMITTER_*`. Nothing else from your
environment goes in.

The image comes from `T12_IMAGE` (default `turbo-12f`, built from
`container/Dockerfile` with `python3`, `git`, and `pytest`). It must
contain every tool your gate commands call, so add yours to the
Dockerfile or point `T12_IMAGE` at your own image. The image must
already be present locally. The launcher does not pull or build it. Verification inside the container uses Landlock, as it does on
the host. The `docker` sandbox adapter is not available inside the
container, and a task that requests it is refused at `prepare`.
Reaching Docker from inside would mean mounting the Docker socket,
which gives the container root on the host.

## Tests

```sh
python3 -m unittest discover -s tests -v
bash scripts/smoke.sh
T12=bin/t12-container bash scripts/smoke.sh   # the same smoke, in a container
```

All three run against disposable repos only. The docker adapter tests
run against a fake `docker` stub, and also against the real daemon
when Docker answers and the `python:3.12` image is present locally.
