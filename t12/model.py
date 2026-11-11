"""The run record: one task, one record, execution state and business
state unified (Factor 5). The state machine is the only control flow
(Factor 8); each CLI invocation is a stateless reducer over the record
on disk (Factor 12): load state, apply one event, persist atomically.
"""

import contextlib
import fcntl
import json
import os
import re
import time

from . import limits as limits_mod
from . import store
from .errors import BudgetError, StateError

VERSION = 1
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
PROVENANCE = "observed-helper-execution"

# Roles that run as separate worker invocations. A session id may be
# bound to exactly one role per run: the same worker cannot write the
# tests and the implementation, or review its own code.
SESSION_ROLES = ("test-writer", "builder", "qa", "reviewer", "security")
REVIEW_ROLES = ("reviewer", "security")

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
    # green -> gates is the review loop: after replay, a review can
    # ask for changes, so the builder re-stages a new candidate.
    # The re-stage voids the review and resets gates + replay.
    "green": {"gates", "local-verified", "paused", "stopped"},
    "local-verified": set(),
    "paused": {"ready", "red", "gates", "green", "stopped"},
    "stopped": set(),
}


def run_path(home, task_id):
    return os.path.join(home, "runs", f"{task_id}.json")


def events_path(home, task_id):
    return os.path.join(home, "runs", f"{task_id}.events.jsonl")


def lock_path(home, task_id):
    return os.path.join(home, "runs", f"{task_id}.lock")


def is_busy(home, task_id):
    """True while another command holds the run's lock."""
    try:
        fd = os.open(lock_path(home, task_id), os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def run_lock(home, task_id):
    """Hold the run's lock for one command. The kernel releases it when
    the process exits, so a crashed command never leaves it held."""
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise StateError(f"task id must match {TASK_ID_RE.pattern}: {task_id!r}")
    path = lock_path(home, task_id)
    if not os.path.isdir(os.path.dirname(path)):
        raise StateError(f"no turbo-12f state home at {home}; run `init` first")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateError(f"run {task_id!r} is busy: another t12 command holds "
                             f"{path}; retry when it finishes") from None
        yield
    finally:
        os.close(fd)


def verify_checkout_dir(home, task_id):
    return os.path.join(home, "verify", task_id)


def evidence_dir(home, task_id):
    return os.path.join(home, "evidence", task_id)


def bound_sessions(run):
    """All session ids bound so far, as {session: role}."""
    bound = {}
    for role, record in (run.get("roles") or {}).items():
        bound[record["session"]] = role
    for gate in run["gates"]:
        if gate.get("run_by"):
            bound[gate["run_by"]] = "qa"
    for review in run.get("reviews") or []:
        bound[review["session"]] = review["role"]
    return bound


def check_session(run, role, session):
    """One session per role per run. A session already bound to a
    different role cannot take this one — that would collapse the
    separation the roles exist to enforce."""
    if not session or not str(session).strip():
        raise StateError(f"--session is required for the {role} role")
    bound = bound_sessions(run)
    other = bound.get(session)
    if other is not None and other != role:
        raise StateError(
            f"session {session!r} is already bound to the {other} role; "
            f"a separate worker session is required for {role}"
        )


def bind_session(run, role, session):
    check_session(run, role, session)
    run.setdefault("roles", {})[role] = {"session": session, "at": time.time()}


def latest_review(run, role, commit=None):
    """The latest non-voided review for a role, optionally on one commit."""
    for review in reversed(run.get("reviews") or []):
        if review["role"] != role or review["voided"]:
            continue
        if commit is not None and review["commit"] != commit:
            continue
        return review
    return None


def check_reviews(run):
    """Green requires an approving review for every required role, on
    the current candidate. A changes-requested review newer than the
    latest approval blocks; a missing review blocks."""
    candidate = (run["candidate"] or {}).get("commit")
    if not candidate:
        raise StateError("green requires a staged candidate")
    for role in run.get("required_reviews") or []:
        review = latest_review(run, role, commit=candidate)
        if review is None:
            raise StateError(
                f"green requires an approving {role} review on {candidate[:12]}; none recorded"
            )
        if review["verdict"] != "approve":
            raise StateError(
                f"green blocked: the latest {role} review on {candidate[:12]} "
                f"is {review['verdict']!r}"
            )


def next_step(run):
    """Who acts next on this run, and on what, as one short line."""
    phase = run["phase"]
    if phase == "ready":
        return "test-writer: red"
    if phase == "red":
        return "builder: stage"
    if phase == "gates":
        for gate in run["gates"]:
            if gate["status"] == "failed":
                return f"builder: repair gate {gate['name']}"
            if gate["status"] != "passed":
                return f"qa: gate {gate['name']}"
        return "replay"
    if phase == "green":
        commit = run["candidate"]["commit"]
        for role in run.get("required_reviews") or []:
            review = latest_review(run, role, commit=commit)
            if review is None:
                return f"{role}: review"
            if review["verdict"] != "approve":
                return f"builder: changes requested by {role}"
        return "green"
    if phase == "local-verified":
        return "human: merge"
    if phase == "paused":
        pending = open_escalation(run)
        if pending:
            return f"human: answer {pending['question']!r}"
        return f"human: resume (was {run['prior_phase']})"
    if phase == "stopped":
        return f"stopped: {run['stop_reason']}"
    return "prepare"


def void_reviews(run, reason):
    """A re-stage voids reviews bound to the old candidate. They stay
    in the record (audit trail) and the next review for the role
    starts from the voided one — review remembers."""
    for review in run.get("reviews") or []:
        if not review["voided"]:
            review["voided"] = True
            review["voided_reason"] = reason


def record_measurements(run):
    """Snapshot what a run actually used, at a terminal transition.
    Calibration reads these; a run without measurements cannot
    calibrate anything and is reported as such."""
    budgets = run["budgets"]
    run["measurements"] = {
        "wall_used_s": round(time.time() - budgets["started_at"], 1),
        "ops_used": budgets["ops_used"],
        "repairs_used": budgets["repairs_used"],
        "gates": len(run["gates"]),
        "finished_at": time.time(),
    }
    return run["measurements"]


def _is_worktree_path(path):
    if not isinstance(path, str) or not path or os.path.isabs(path):
        return False
    first = os.path.normpath(path).split(os.sep)[0]
    return first not in ("..", ".", ".git")


def open_escalation(run):
    escalations = run.get("escalations") or []
    if escalations and escalations[-1]["answer"] is None:
        return escalations[-1]
    return None


def new_run(task, repo, home, supersedes=None, defaults=None,
            defaults_source="builtin", sandbox_adapter=None, sandbox_image=None):
    now = time.time()
    defaults = defaults or limits_mod.BUILTIN_DEFAULTS
    budget_source = defaults_source
    limits = {}
    for key in limits_mod.KEYS:
        if key in task:
            limits[key] = int(task[key])
            budget_source = "task"
        else:
            limits[key] = int(defaults.get(key, limits_mod.BUILTIN_DEFAULTS[key]))
    gates = []
    for g in task.get("gates", []):
        if not isinstance(g.get("command"), list) or not g["command"]:
            raise StateError(f"gate {g.get('name')!r} must have a non-empty argv command")
        gates.append(
            {"name": g["name"], "command": list(g["command"]), "status": "pending",
             "observation": None, "commit": None}
        )
    if not gates:
        raise StateError("task must declare at least one gate")
    if not task.get("acceptance_criteria"):
        raise StateError("task must declare at least one acceptance criterion")
    required_reviews = task.get("required_reviews", ["reviewer"])
    if not isinstance(required_reviews, list) or any(
            r not in REVIEW_ROLES for r in required_reviews):
        raise StateError(
            f"required_reviews must be a list of {list(REVIEW_ROLES)}: {required_reviews!r}"
        )
    verify_deps = task.get("verify_deps", [])
    if not isinstance(verify_deps, list) or not all(_is_worktree_path(d) for d in verify_deps):
        raise StateError(
            f"verify_deps must be a list of relative paths inside the worktree: {verify_deps!r}"
        )
    normalized = [os.path.normpath(d) for d in verify_deps]
    for i, a in enumerate(normalized):
        for b in normalized[i + 1:]:
            if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
                raise StateError(f"verify_deps entries overlap: {a!r} and {b!r}")
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
            "time_limit_s": limits["time_limit_s"],
            "op_limit": limits["op_limit"],
            "repair_cap": limits["repair_cap"],
            "started_at": now,
            "ops_used": 0,
            "repairs_used": 0,
        },
        "budget_source": budget_source,
        "measurements": None,
        "gates": gates,
        "red": None,
        "candidate": None,
        "replay": None,
        "stop_reason": None,
        "require_probes": bool(task.get("require_probes", False)),
        "probes": [],
        "sandbox_adapter": sandbox_adapter,
        "sandbox_image": sandbox_image,
        "roles": {},
        "reviews": [],
        "required_reviews": list(required_reviews),
        "verify_deps": normalized,
        "escalations": [],
        "deliveries": [],
        "github_repo": task.get("github_repo"),
        "asana_task_gid": task.get("asana_task_gid"),
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
