#!/usr/bin/env python3
"""Drive runs to local-verified, or to a human, with no one in the inner loop.

Plain code routes every step (Factor 8). Each pass admits queued tasks
through `t12-queue`, then takes each run's `next` from `t12 board --json`:

- `replay` and `green` run as `t12` commands, and `qa: gate N` runs
  `t12 gate` under a fresh QA session. No model is called.
- Any other `<role>: ...` is a worker step. `t12 context` builds the
  prompt, one fresh model call answers it under a new session id, and
  `t12 act` takes the JSON. A rejected output goes back to the same
  session once, with the error.
- `human: ...`, `stopped: ...` and a spent budget are reported and
  skipped. The runner never answers an escalation and never merges.

The runner writes no state. The run records and event logs are the
only state, so it can be killed at any point and started again
anywhere. Within one invocation it remembers which runs a step did not
move and leaves them alone, and it stops when a pass moves no run.

Host-side, like the other adapters: it is the one piece that calls a
model, and the core never imports it.
"""

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
CONTEXT_MARKER = "--- context (JSON) ---"
DETERMINISTIC = ("replay", "green")


def t12(*args, timeout=None):
    return subprocess.run([os.path.join(BIN, "t12"), *args],
                          capture_output=True, text=True, timeout=timeout)


def board(home):
    result = t12("board", "--home", home, "--json")
    if result.returncode != 0:
        raise SystemExit(f"error: board failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def last_event_type(home, task_id):
    with open(os.path.join(home, "runs", f"{task_id}.events.jsonl"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    return json.loads(lines[-1])["type"] if lines else None


def ops_left(home, task_id):
    with open(os.path.join(home, "runs", f"{task_id}.json"), encoding="utf-8") as fh:
        budgets = json.load(fh)["budgets"]
    return budgets["op_limit"] - budgets["ops_used"]


def position(row):
    """Where a run stands. A step that leaves this unchanged did not move
    the run, and taking it again would not either."""
    return row["phase"], row["next"], row["candidate"]


def waiting_reason(row):
    """Why no step can be taken for this run, or None if one can."""
    nxt = row["next"]
    if nxt.startswith(("human: ", "stopped: ")):
        return nxt
    if row["busy"]:
        return None
    for key, label in (("time_left_s", "time"), ("ops_left", "operation"),
                       ("repairs_left", "repair")):
        floor = 0 if key == "repairs_left" else 1
        if row[key] is not None and row[key] < floor:
            return f"{label} budget exhausted; stop it or prepare it again with --supersede"
    return None


def call_model(model_cmd, prompt, env, cwd, timeout):
    try:
        result = subprocess.run(model_cmd, input=prompt, capture_output=True, text=True,
                                cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    if result.returncode != 0:
        return None, f"exit {result.returncode}: {result.stderr.strip()[-500:]}"
    return result.stdout, None


def worker_step(home, row, role, model_cmd, scratch):
    task_id = row["task_id"]
    ctx = t12("context", "--home", home, "--task-id", task_id,
              "--role", role, "--phase", row["phase"])
    if ctx.returncode != 0:
        return f"context failed: {ctx.stderr.strip()}"
    worktree = json.loads(ctx.stdout.rsplit(CONTEXT_MARKER, 1)[1])["worktree"]
    session = f"{role}-{uuid.uuid4().hex[:16]}"
    env = dict(os.environ, T12_TASK_ID=task_id, T12_ROLE=role, T12_SESSION=session,
               T12_WORKTREE=worktree)
    prompt = ctx.stdout
    for attempt in (1, 2):
        output, failure = call_model(model_cmd, prompt, env, worktree,
                                     max(1, row["time_left_s"] or 1))
        if failure:
            return f"{role} model call failed ({failure}); no output recorded"
        out_path = os.path.join(scratch, f"{task_id}-{session}-{attempt}.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(output)
        act = t12("act", "--home", home, "--task-id", task_id, "--role", role,
                  "--session", session, "--input", out_path)
        if act.returncode == 0:
            return act.stdout.strip()
        error = act.stderr.strip()
        if (attempt == 2 or last_event_type(home, task_id) != "worker-output-rejected"
                or ops_left(home, task_id) < 1):
            return f"{role} step failed: {error}"
        prompt = (ctx.stdout + "\n--- your previous output was rejected ---\n" + error
                  + "\nReply with only the corrected JSON tool call.\n")


def step(home, row, model_cmd, scratch):
    """Take the one step `next` names. Returns a line for the log."""
    nxt = row["next"]
    if nxt in DETERMINISTIC:
        result = t12(nxt, "--home", home, "--task-id", row["task_id"])
        return (result.stdout or result.stderr).strip()
    role, _, what = nxt.partition(": ")
    if what.startswith("gate "):
        result = t12("gate", "--home", home, "--task-id", row["task_id"],
                     "--name", what[len("gate "):], "--role", role,
                     "--session", f"{role}-{uuid.uuid4().hex[:16]}")
        return (result.stdout or result.stderr).strip()
    return worker_step(home, row, role, model_cmd, scratch)


def notify(notify_cmd, message):
    print(message)
    if notify_cmd:
        try:
            subprocess.run([*notify_cmd, message], timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"warning: notify failed: {exc}", file=sys.stderr)


def drive(home, queue, max_active, model_cmd, notify_cmd):
    """Pass after pass until no step moves a run. A run whose step left it
    where it was is not stepped again in this invocation, so a worker that
    cannot get past a refusal costs one call per invocation, not a loop."""
    stuck = {}
    with tempfile.TemporaryDirectory(prefix="t12-runner-") as scratch:
        moved = True
        while moved:
            moved = False
            if queue:
                admitted = subprocess.run(
                    [os.path.join(BIN, "t12-queue"), "--home", home, "--queue", queue,
                     "--max", str(max_active)], capture_output=True, text=True)
                for line in (admitted.stdout + admitted.stderr).splitlines():
                    print(f"queue: {line}")
                    moved = moved or line.startswith("started ")
            for row in board(home):
                if row["busy"] or waiting_reason(row) or row["task_id"] in stuck:
                    continue
                line = step(home, row, model_cmd, scratch)
                print(f"{row['task_id']}: {row['next']} -> {line}")
                after = {r["task_id"]: r for r in board(home)}[row["task_id"]]
                if position(after) == position(row):
                    stuck[row["task_id"]] = f"{row['next']} did not move the run: {line}"
                else:
                    moved = True
    for row in board(home):
        reason = waiting_reason(row) or stuck.get(row["task_id"])
        if reason:
            notify(notify_cmd, f"waiting {row['task_id']}: {reason}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="runner", description=__doc__.splitlines()[0])
    parser.add_argument("--home", required=True)
    parser.add_argument("--model-cmd", required=True,
                        help="command for one worker call, e.g. 'claude -p': the prompt on "
                             "stdin, only the JSON tool call on stdout")
    parser.add_argument("--queue", help="directory of task files to admit each pass")
    parser.add_argument("--max", type=int, default=1, help="most runs active at once")
    parser.add_argument("--notify-cmd",
                        help="command run with each waiting message as its last argument")
    args = parser.parse_args(argv)
    home = os.path.abspath(args.home)
    with open(os.path.join(home, "runner.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("error: another runner is driving this state home", file=sys.stderr)
            return 1
        drive(home, args.queue and os.path.abspath(args.queue), args.max,
              shlex.split(args.model_cmd),
              shlex.split(args.notify_cmd) if args.notify_cmd else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
