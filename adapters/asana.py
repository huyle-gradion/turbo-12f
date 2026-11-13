#!/usr/bin/env python3
"""Asana delivery adapter (Phase 7).

Marks a task complete with a comment carrying the manifest summary
and the reviewed head SHA — and then reads the task back to confirm.

Usage:
    python3 adapters/asana.py complete --home <state-home> --task-id <id> \
        --out report.json [--task <asana-gid>] \
        [--api-base https://app.asana.com/api/1.0] [--timeout 10]

Token from ASANA_TOKEN; the Asana task GID comes from --task or the
task file's `asana_task_gid` field. Exit 0 means a report was written
(whatever the outcome); non-zero means the adapter could not run.

Outcome rules:
- Task already complete -> confirmed (no duplicate comment).
- Comment/complete returns 403 -> denied. 404 -> rejected.
  5xx/timeout -> uncertain.
- Read-back after completing shows incomplete, or the read fails ->
  uncertain (the mutation may or may not have landed; the next run
  reads back instead of repeating the mutation blind).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    manifest_text, mutation_outcome, read_outcome, read_run, report,
    request, write_report,
)


def complete(home, task_id, gid, api_base, timeout, token):
    run = read_run(home, task_id)
    candidate = (run.get("candidate") or {}).get("commit")
    if not candidate:
        raise SystemExit("error: run has no staged candidate")
    manifest = manifest_text(home, task_id)

    def fail(outcome, detail, ref=None):
        return report("asana", task_id, candidate, outcome, ref, detail)

    def read_task():
        status, body, error = request(
            "GET", f"{api_base}/tasks/{gid}?opt_fields=completed,name,permalink_url",
            token, timeout=timeout)
        if status == 404:
            return None, "rejected", f"Asana task {gid} not found"
        if status != 200 or not isinstance(body, dict):
            outcome, detail = read_outcome(status, error)
            return None, outcome, f"could not read Asana task: {detail}"
        return body.get("data") or {}, None, None

    # 1. Read first: an already-complete task needs no mutation.
    task, outcome, detail = read_task()
    if task is None:
        return fail(outcome, detail)
    if task.get("completed"):
        return fail("confirmed", f"Asana task {gid} is already complete",
                    ref=task.get("permalink_url"))

    # 2. Comment with the delivery evidence.
    text = (
        f"Delivered by turbo-12f: {run['title']}\n"
        f"Candidate: {candidate}\n"
        f"Acceptance criteria: {'; '.join(run.get('acceptance_criteria') or [])}\n"
        + (f"\n{manifest}" if manifest else "")
    )
    status, _body, error = request(
        "POST", f"{api_base}/tasks/{gid}/stories", token,
        {"data": {"text": text}}, timeout=timeout)
    if status != 201:
        outcome, detail = mutation_outcome(status, error, _body)
        return fail(outcome, f"could not comment on Asana task: {detail}")

    # 3. Mark complete.
    status, _body, error = request(
        "PUT", f"{api_base}/tasks/{gid}", token,
        {"data": {"completed": True}}, timeout=timeout)
    if status != 200:
        outcome, detail = mutation_outcome(status, error, _body)
        return fail(outcome, f"could not complete Asana task: {detail}")

    # 4. Read it back: only a completed task is a confirmed delivery.
    task, outcome, detail = read_task()
    if task is None:
        return fail(outcome, detail)
    if not task.get("completed"):
        return fail("uncertain",
                    f"Asana task {gid} still shows incomplete after completing it",
                    ref=task.get("permalink_url"))
    return fail("confirmed", f"Asana task {gid} completed",
                ref=task.get("permalink_url"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Asana delivery adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("complete")
    p.add_argument("--home", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--task", help="Asana task GID (default: task file asana_task_gid)")
    p.add_argument("--out", required=True)
    p.add_argument("--api-base", default="https://app.asana.com/api/1.0")
    p.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    token = os.environ.get("ASANA_TOKEN")
    if not token:
        print("error: ASANA_TOKEN is not set", file=sys.stderr)
        return 2
    run = read_run(args.home, args.task_id)
    gid = args.task or run.get("asana_task_gid")
    source = run.get("source") or {}
    if not gid and source.get("adapter") == "asana-tasks":
        gid = str(source.get("ref") or "").rpartition("#")[2] or None
    if not gid:
        print("error: cannot determine the Asana task; pass --task or set asana_task_gid in the task",
              file=sys.stderr)
        return 2
    data = complete(args.home, args.task_id, gid, args.api_base.rstrip("/"),
                    args.timeout, token)
    write_report(args.out, data)
    print(f"asana {data['outcome']}" + (f" ref={data['ref']}" if data["ref"] else ""))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
