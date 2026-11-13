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
- A `local-verified` run whose profile names a `delivery` adapter is
  pushed, opened as a change request, and recorded with `t12 deliver`.
  Without one the runner says it is ready to deliver by hand. A failed
  step prints the command that retries it; an `uncertain` delivery is
  never repeated and goes to `--notify-cmd` for `--resolves`.
- `human: ...`, `stopped: ...` and a spent budget are reported and
  skipped. The runner never answers an escalation and never merges.

The tool call is taken from the reply in one of two ways, and `act`
records which along with the whole reply. A model CLI that can enforce
a JSON Schema while the model writes (`claude`, `codex exec`) is given
the role's schema from `t12 tool-schema`. Any other reply, or a schema
call with no structured result, is searched for exactly one tool call,
bare or fenced; none or several distinct ones pass the reply on as it
is. Either way `t12 act` checks the call against the registry.

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
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
CONTEXT_MARKER = "--- context (JSON) ---"
DETERMINISTIC = ("replay", "green")
ADAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
FORGES = {"github": ("create-pr", "GITHUB_TOKEN"),
          "gitlab": ("create-mr", "GITLAB_TOKEN"),
          "bitbucket": ("create-pr", "BITBUCKET_TOKEN")}


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


def schema_cli(model_cmd):
    """The model CLI whose schema flags the runner knows, or None."""
    name = os.path.basename(model_cmd[0])
    if name in ("claude", "agy") or (name == "codex" and model_cmd[1:2] == ["exec"]):
        return name
    return None


def with_schema(model_cmd, cli, schema_path, result_path):
    """model_cmd with the flags that make its CLI enforce the schema. They
    go right after the program (or its subcommand): a flag such as
    `-p` may take the next word as its value."""
    with open(schema_path, encoding="utf-8") as fh:
        schema = fh.read()
    if cli == "claude":
        return [model_cmd[0], "--output-format", "json", "--json-schema", schema,
                *model_cmd[1:]]
    if cli == "agy":
        # agy rejects --json-schema unless the output format is json.
        return [model_cmd[0], "--output-format", "json", "--json-schema", schema,
                *model_cmd[1:]]
    return [*model_cmd[:2], "--output-schema", schema_path, "-o", result_path,
            *model_cmd[2:]]


def structured_result(cli, stdout, result_path):
    """(the tool call the CLI enforced or None, the reply's text)."""
    if cli in ("claude", "agy"):
        try:
            envelope = json.loads(stdout)
        except ValueError:
            return None, stdout
        if not isinstance(envelope, dict):
            return None, stdout
        result = envelope.get("structured_output")
        text = envelope.get("result" if cli == "claude" else "response")
        return (json.dumps(result) if isinstance(result, dict) else None,
                text if isinstance(text, str) else stdout)
    try:
        with open(result_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, stdout
    try:
        return (text if isinstance(json.loads(text), dict) else None), text
    except ValueError:
        return None, text


FENCED = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)```", re.S)
LINE_START_OBJECT = re.compile(r"(?m)^[ \t]*\{")


def extract_tool_call(text):
    """The one tool call in a reply that wraps it in prose or a fence, or
    prints it more than once, or None when there is none or there are
    two that differ. Never repairs JSON: it only finds it."""
    decoder = json.JSONDecoder()
    starts = [block.lstrip() for block in FENCED.findall(text)]
    starts += [text[m.end() - 1:] for m in LINE_START_OBJECT.finditer(text)]
    calls = {}
    for start in starts:
        try:
            value, _ = decoder.raw_decode(start)
        except ValueError:
            continue
        if isinstance(value, dict) and "tool" in value:
            calls[json.dumps(value, sort_keys=True)] = value
    return json.dumps(next(iter(calls.values()))) if len(calls) == 1 else None


def is_json(text):
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def tool_call(cli, output, result_path):
    """(the JSON to hand `act`, how it was got)."""
    structured, text = structured_result(cli, output, result_path) if cli else (None, output)
    if structured is not None:
        return structured, "schema"
    if text == output and is_json(output):
        return output, "raw"
    extracted = extract_tool_call(text)
    return (extracted, "extracted") if extracted is not None else (output, "raw")


def context_json(stdout):
    """The context JSON after the last marker line. The marker must end the
    line: a context string (a carried patch, a prompt quoting this code) can
    hold the marker text, but JSON escapes its newlines, so only the real
    marker line is followed by a raw one."""
    return json.loads(stdout.rsplit(CONTEXT_MARKER + "\n", 1)[1])


def worker_step(home, row, role, model_cmd, scratch):
    task_id = row["task_id"]
    ctx = t12("context", "--home", home, "--task-id", task_id,
              "--role", role, "--phase", row["phase"])
    if ctx.returncode != 0:
        return f"context failed: {ctx.stderr.strip()}"
    worktree = context_json(ctx.stdout)["worktree"]
    session = f"{role}-{uuid.uuid4().hex[:16]}"
    env = dict(os.environ, T12_TASK_ID=task_id, T12_ROLE=role, T12_SESSION=session,
               T12_WORKTREE=worktree)
    cli = schema_cli(model_cmd)
    schema_path = os.path.join(scratch, f"{task_id}-{session}.schema.json")
    if cli:
        schema = t12("tool-schema", "--home", home, "--task-id", task_id, "--role", role)
        if schema.returncode != 0:
            return f"tool-schema failed: {schema.stderr.strip()}"
        with open(schema_path, "w", encoding="utf-8") as fh:
            fh.write(schema.stdout)
    prompt = ctx.stdout
    for attempt in (1, 2):
        base = os.path.join(scratch, f"{task_id}-{session}-{attempt}")
        result_path = base + ".result.json"
        argv = with_schema(model_cmd, cli, schema_path, result_path) if cli else model_cmd
        output, failure = call_model(argv, prompt, env, worktree,
                                     max(1, row["time_left_s"] or 1))
        if failure and cli and failure.startswith("exit "):
            print(f"warning: {cli} with the tool-call schema failed ({failure}); "
                  "calling it again without the schema", file=sys.stderr)
            cli, argv = None, model_cmd
            output, failure = call_model(argv, prompt, env, worktree,
                                         max(1, row["time_left_s"] or 1))
        if failure:
            return f"{role} model call failed ({failure}); no output recorded"
        payload, parsed_by = tool_call(cli, output, result_path)
        out_path, raw_path = base + ".json", base + ".raw.txt"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        with open(raw_path, "w", encoding="utf-8") as fh:
            fh.write(output)
        extra = [] if payload == output else ["--raw", raw_path]
        act = t12("act", "--home", home, "--task-id", task_id, "--role", role,
                  "--session", session, "--input", out_path,
                  "--parsed-by", parsed_by, *extra)
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


def read_run(home, task_id):
    with open(os.path.join(home, "runs", f"{task_id}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def deliver(home, task_id, notify_cmd, scratch):
    """Push a local-verified run and open its change request. Never
    merges, approves or closes. It reports and returns nothing."""
    run = read_run(home, task_id)
    adapter = run.get("delivery_adapter")
    if run["phase"] != "local-verified" or not adapter:
        return
    last = (run.get("deliveries") or [None])[-1]
    if last and last["outcome"] == "confirmed":
        return
    if last and last["outcome"] == "uncertain":
        notify(notify_cmd, f"{task_id}: delivery via {adapter} is uncertain "
                           f"({last.get('detail', '')}); read it back and record it with "
                           f"`t12 deliver --resolves`. The runner will not repeat it")
        return
    verb, token_var = FORGES[adapter]
    if not os.environ.get(token_var):
        print(f"{task_id}: not delivered: set {token_var} in the runner's environment")
        return
    os.makedirs(os.path.join(home, "deliveries"), exist_ok=True)
    report = os.path.join(home, "deliveries", f"{task_id}.json")
    steps = (
        ("push", ["git", "push", "origin", run["branch"]]),
        (adapter, [sys.executable, os.path.join(ADAPTER_DIR, f"{adapter}.py"), verb,
                   "--home", home, "--task-id", task_id, "--out", report]),
        ("t12 deliver", [os.path.join(BIN, "t12"), "deliver", "--home", home,
                         "--task-id", task_id, "--adapter", adapter, "--report", report]),
    )
    for name, cmd in steps:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=run["worktree"])
        if result.returncode != 0:
            print(f"{task_id}: delivery stopped at {name}: "
                  f"{(result.stderr or result.stdout).strip()}\n"
                  f"{task_id}: retry with: cd {shlex.quote(run['worktree'])} && {shlex.join(cmd)}")
            return
    entry = read_run(home, task_id)["deliveries"][-1]
    if entry["outcome"] == "confirmed":
        print(f"{task_id}: delivered via {adapter}: {entry['ref']}")
    elif entry["outcome"] == "uncertain":
        deliver(home, task_id, notify_cmd, scratch)
    else:
        print(f"{task_id}: {adapter} did not confirm the delivery ({entry['outcome']}: "
              f"{entry.get('detail', '')}); retry with: {shlex.join(steps[0][1])} "
              f"&& {shlex.join(steps[1][1])}")


def drive(home, queue, max_active, model_cmd, notify_cmd):
    """Pass after pass until no step moves a run. A run whose step left it
    where it was is not stepped again in this invocation, so a worker that
    cannot get past a refusal costs one call per invocation, not a loop."""
    stuck, handled = {}, set()
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
                if row["phase"] == "local-verified" and row["task_id"] not in handled:
                    handled.add(row["task_id"])
                    deliver(home, row["task_id"], notify_cmd, scratch)
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
        if row["phase"] == "local-verified" and not read_run(
                home, row["task_id"]).get("delivery_adapter"):
            print(f"{row['task_id']}: local-verified and ready to deliver by hand "
                  "(the profile sets no delivery adapter)")
        reason = waiting_reason(row) or stuck.get(row["task_id"])
        if reason:
            notify(notify_cmd, f"waiting {row['task_id']}: {reason}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="runner", description=__doc__.splitlines()[0])
    parser.add_argument("--home", required=True)
    parser.add_argument("--model-cmd", required=True,
                        help="command for one worker call, e.g. 'claude -p' or 'codex exec': "
                             "the prompt on stdin, the reply on stdout")
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
        model_cmd = shlex.split(args.model_cmd)
        cli = schema_cli(model_cmd)
        print(f"model: {cli} enforces the tool-call schema" if cli else
              f"model: {os.path.basename(model_cmd[0])} has no known schema flag; "
              "tool calls are taken from the reply's text")
        drive(home, args.queue and os.path.abspath(args.queue), args.max, model_cmd,
              shlex.split(args.notify_cmd) if args.notify_cmd else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
