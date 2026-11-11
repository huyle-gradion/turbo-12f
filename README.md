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

# 5. Run the gates in order, replay the red test, certify
bin/t12 gate --home /path/to/state-home --task-id demo-1 --name unit
bin/t12 replay --home /path/to/state-home --task-id demo-1
bin/t12 green --home /path/to/state-home --task-id demo-1
bin/t12 manifest --home /path/to/state-home --task-id demo-1
```

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
| `green` | Re-hash and re-validate all evidence; certify `local-verified` |
| `manifest` | Write the manifest (recorded as an event with its SHA-256) |
| `verify` | Audit the run: event chain, evidence hashes, exit codes, HEAD, manifest |
| `probe` | Run the safety probes; exit 0 only if every probe passes |
| `report` | Budget report across runs: usage vs budget, cap hits, calibration summary (`--json` for scripts) |
| `calibrate` | Recommend defaults from completed runs; `--apply` writes `limits.json` |
| `status` | Show phase, budgets, gates, candidate |
| `pause` / `resume` | Pause with a reason; resume re-validates from disk |
| `stop` | Stop a run explicitly with a reason (terminal; use `--supersede` to start over) |
| `escalate` | Record a human question as a tool call and pause (Factor 7) |

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

Budgets (`time_limit_s`, `op_limit`, `repair_cap`) are optional in
the task file. When omitted, `prepare` uses `limits.json` in the
state home if `t12 calibrate --apply` has written one, else the
built-ins (3600s / 100 ops / 3 repairs). The run records which
source applied (`budget_source`).

## Tests

```sh
python3 -m unittest discover -s tests -v
bash scripts/smoke.sh
```

Both run against disposable repos only.
