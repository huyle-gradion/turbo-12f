"""The run record: one task, one record, execution state and business
state unified (Factor 5). The state machine is the only control flow
(Factor 8); each CLI invocation is a stateless reducer over the record
on disk (Factor 12): load state, apply one event, persist atomically.
"""

import json
import os
import time

from . import store
from .errors import BudgetError, StateError

VERSION = 1
PROVENANCE = "observed-helper-execution"

PHASES = (
    "unadmitted",
    "ready",
    "red",
    "gates",
    "green",
    "local-verified",
    "paused",
    "stopped",
)

# Allowed transitions. "gates -> gates" is a re-stage after a repair:
# it re-pins the candidate and resets gate observations bound to the
# old candidate.
TRANSITIONS = {
    "unadmitted": {"ready"},
    "ready": {"red", "paused", "stopped"},
    "red": {"gates", "paused", "stopped"},
    "gates": {"gates", "green", "paused", "stopped"},
    "green": {"local-verified", "paused", "stopped"},
    "local-verified": set(),
    "paused": {"ready", "red", "gates", "green", "stopped"},
    "stopped": set(),
}


def run_path(home, task_id):
    return os.path.join(home, "runs", f"{task_id}.json")


def events_path(home, task_id):
    return os.path.join(home, "runs", f"{task_id}.events.jsonl")


def evidence_dir(home, task_id):
    return os.path.join(home, "evidence", task_id)


def new_run(task, repo, home, supersedes=None):
    now = time.time()
    gates = []
    for g in task.get("gates", []):
        if not isinstance(g.get("command"), list) or not g["command"]:
            raise StateError(f"gate {g.get('name')!r} must have a non-empty argv command")
        gates.append(
            {"name": g["name"], "command": list(g["command"]), "status": "pending",
             "observation": None}
        )
    if not gates:
        raise StateError("task must declare at least one gate")
    if not task.get("acceptance_criteria"):
        raise StateError("task must declare at least one acceptance criterion")
    return {
        "version": VERSION,
        "task_id": task["id"],
        "title": task.get("title", task["id"]),
        "acceptance_criteria": list(task["acceptance_criteria"]),
        "repo": repo,
        "base": task.get("base", "main"),
        "branch": task.get("branch") or f"task/{task['id']}",
        "worktree": os.path.join(home, "worktrees", task["id"]),
        "phase": "unadmitted",
        "prior_phase": None,
        "supersedes": supersedes,
        "created_at": now,
        "config_digest": store.sha256_bytes(store.canonical_json(task)),
        "budgets": {
            "time_limit_s": int(task.get("time_limit_s", 3600)),
            "op_limit": int(task.get("op_limit", 100)),
            "repair_cap": int(task.get("repair_cap", 3)),
            "started_at": now,
            "ops_used": 0,
            "repairs_used": 0,
        },
        "gates": gates,
        "red": None,
        "candidate": None,
        "replay": None,
        "stop_reason": None,
        "require_probes": bool(task.get("require_probes", False)),
        "probes": [],
        "sandbox_adapter": None,
    }


def load_run(home, task_id):
    path = run_path(home, task_id)
    if not os.path.exists(path):
        raise StateError(f"no run for task {task_id!r} in {home}")
    run = store.load_json(path)
    if run.get("version") != VERSION:
        raise StateError(f"unsupported run version: {run.get('version')}")
    return run


def archive_run(home, run):
    """Supersession, not reset: move the old run record and its event
    log aside under a timestamped name. Nothing is overwritten or
    deleted; the archived record keeps every SHA and evidence ref."""
    ts = int(time.time())
    base = f"{run['task_id']}.superseded-{ts}"
    os.replace(run_path(home, run["task_id"]),
               os.path.join(home, "runs", f"{base}.json"))
    ev_src = events_path(home, run["task_id"])
    if os.path.exists(ev_src):
        os.replace(ev_src, os.path.join(home, "runs", f"{base}.events.jsonl"))
    return base


def save_run(home, run):
    store.save_json(run_path(home, run["task_id"]), run)


def genesis_prev(task_id):
    return store.sha256_bytes(f"t12-genesis:{task_id}".encode())


def read_events(home, task_id):
    """Load the event log. Raises StateError on a missing or malformed
    log — a log that cannot be read cannot be trusted."""
    path = events_path(home, task_id)
    if not os.path.exists(path):
        raise StateError(f"no event log for task {task_id!r}")
    events = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StateError(f"event log line {lineno} is not valid JSON: {exc}") from exc
            if not isinstance(event, dict):
                raise StateError(f"event log line {lineno} is not an object")
            events.append(event)
    return events


def append_event(home, run, event_type, detail=None):
    """Append one event to the hash-chained log and return it.

    Each event links to the previous event's hash, carries its
    sequence number, and records a digest of the run record as it
    stands after this command. Editing, deleting, or reordering an
    event breaks the chain; editing the run record after the last
    event breaks the state digest. The chain is the audit trail;
    the run record is the current state."""
    events = []
    path = events_path(home, run["task_id"])
    if os.path.exists(path):
        events = read_events(home, run["task_id"])
    prev = events[-1]["hash"] if events else genesis_prev(run["task_id"])
    event = {
        "seq": len(events) + 1,
        "ts": time.time(),
        "task_id": run["task_id"],
        "phase": run["phase"],
        "type": event_type,
        "detail": detail or {},
        "prev": prev,
        "state_sha": store.sha256_bytes(store.canonical_json(run)),
    }
    event["hash"] = store.sha256_bytes(store.canonical_json(event))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def verify_chain(home, run):
    """Check the event log against the run record. Returns a list of
    problems; empty means the log is intact and the run record is
    exactly what the last event recorded."""
    problems = []
    try:
        events = read_events(home, run["task_id"])
    except StateError as exc:
        return [str(exc)]
    if not events:
        return ["event log is empty"]
    prev = genesis_prev(run["task_id"])
    for index, event in enumerate(events):
        if event.get("seq") != index + 1:
            problems.append(f"event {index + 1}: seq is {event.get('seq')}, expected {index + 1}")
        if event.get("task_id") != run["task_id"]:
            problems.append(f"event {index + 1}: task_id mismatch")
        if event.get("prev") != prev:
            problems.append(f"event {index + 1}: prev link broken")
        recorded = event.get("hash")
        content = {k: v for k, v in event.items() if k != "hash"}
        if recorded != store.sha256_bytes(store.canonical_json(content)):
            problems.append(f"event {index + 1}: hash mismatch (edited?)")
        prev = recorded
    last = events[-1]
    if last.get("state_sha") != store.sha256_bytes(store.canonical_json(run)):
        problems.append("run record changed since the last event (state digest mismatch)")
    return problems


def transition(run, to):
    allowed = TRANSITIONS.get(run["phase"], set())
    if to == run["phase"]:
        return run
    if to not in allowed:
        raise StateError(
            f"cannot move {run['task_id']} from {run['phase']} to {to}; "
            f"allowed: {sorted(allowed) or ['none']}"
        )
    run["phase"] = to
    return run


def check_budgets(run):
    """Fail closed when a budget is exhausted. Call before any step."""
    budgets = run["budgets"]
    wall = time.time() - budgets["started_at"]
    if wall > budgets["time_limit_s"]:
        raise BudgetError(
            f"time budget exhausted: {wall:.0f}s used of {budgets['time_limit_s']}s"
        )
    if budgets["ops_used"] >= budgets["op_limit"]:
        raise BudgetError(
            f"operation budget exhausted: {budgets['ops_used']} of {budgets['op_limit']}"
        )
    if budgets["repairs_used"] > budgets["repair_cap"]:
        raise BudgetError(
            f"repair cap exceeded: {budgets['repairs_used']} of {budgets['repair_cap']}"
        )
    return {"wall_used_s": round(wall, 1)}


def time_left_s(run):
    return max(0, run["budgets"]["time_limit_s"] - (time.time() - run["budgets"]["started_at"]))


def bump_op(run):
    run["budgets"]["ops_used"] += 1
