#!/usr/bin/env python3
"""GitLab delivery adapter (Phase 7).

Creates a merge request for a local-verified run's factory branch —
and never merges. The branch must already be pushed; the adapter
only reads and creates the MR, then reads it back.

Usage:
    python3 adapters/gitlab.py create-mr --home <state-home> --task-id <id> \
        --out report.json [--project group/name] [--title T] [--body B] \
        [--timeout 10]

Token from GITLAB_TOKEN, base URL from GITLAB_URL (default
https://gitlab.com); the REST v4 API is {GITLAB_URL}/api/v4. Exit 0
means a report was written (whatever the outcome); non-zero means the
adapter could not run at all.

Outcome rules:
- MR already open for the branch with the reviewed head -> confirmed
  (no duplicate).
- MR exists with a different head -> rejected.
- Branch not on the remote -> rejected (push it first).
- Create returns 403 -> denied. Other definite 4xx -> rejected.
  5xx/timeout -> uncertain.
- Read-back after create fails or disagrees -> uncertain (the MR may
  exist; the next run finds it instead of creating a duplicate).
- Any failed read -> uncertain, never denied or rejected.
"""

import argparse
import os
import sys
from urllib.parse import quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    break_issue_refs, change_request_body, missing_branch_outcome, mutation_outcome, origin_url, parse_remote,
    read_outcome, read_run, report, request, write_report,
)


def resolve_project(run, explicit, base_url):
    if explicit:
        return explicit
    if run.get("gitlab_project"):
        return run["gitlab_project"]
    remote = parse_remote(origin_url(run))
    if remote and remote[0] == (urlparse(base_url).hostname or "").lower():
        return remote[1]
    return None


def create_mr(home, task_id, project, title, body, base_url, timeout, token):
    run = read_run(home, task_id)
    candidate = (run.get("candidate") or {}).get("commit")
    if not candidate:
        raise SystemExit("error: run has no staged candidate")
    branch = run["branch"]
    if body is None:
        body = change_request_body(home, task_id, run, candidate, "gitlab-issues", project)
    if title is None:
        title = break_issue_refs(run["title"])
    api = f"{base_url}/api/v4/projects/{quote(project, safe='')}"
    encoded_branch = quote(branch, safe="")

    def done(outcome, detail, ref=None):
        return report("gitlab", task_id, candidate, outcome, ref, detail)

    status, _body, error = request(
        "GET", f"{api}/repository/branches/{encoded_branch}", token, timeout=timeout)
    if status == 404:
        project_status, _, _ = request("GET", api, token, timeout=timeout)
        return done(*missing_branch_outcome(branch, project_status))
    if status != 200:
        outcome, detail = read_outcome(status, error)
        return done(outcome, f"could not check remote branch: {detail}")

    status, listing, error = request(
        "GET", f"{api}/merge_requests?state=opened&source_branch={encoded_branch}"
               f"&target_branch={quote(run['base'], safe='')}",
        token, timeout=timeout)
    if status != 200 or not isinstance(listing, list):
        outcome, detail = read_outcome(status, error)
        return done(outcome, f"could not list merge requests: {detail}")
    if listing:
        iid = listing[0].get("iid")
        if not iid:
            return done("uncertain", "could not read the existing merge request iid")
        status, mr, error = request(
            "GET", f"{api}/merge_requests/{iid}", token, timeout=timeout)
        if status != 200 or not isinstance(mr, dict):
            outcome, detail = read_outcome(status, error)
            return done(outcome, f"could not read back MR !{iid}: {detail}")
        head_sha = mr.get("sha")
        if head_sha == candidate and mr.get("target_branch") != run["base"]:
            return done("rejected", f"MR !{iid} has the reviewed head but targets "
                        f"{mr.get('target_branch')!r}, not {run['base']!r}",
                        ref=mr.get("web_url"))
        if head_sha == candidate:
            return done("confirmed", f"MR !{iid} already exists with the reviewed head",
                        ref=mr.get("web_url"))
        return done("rejected",
                    f"MR !{iid} exists but its head is {str(head_sha)[:12]}, "
                    f"not the reviewed head {candidate[:12]}",
                    ref=mr.get("web_url"))

    status, created, error = request(
        "POST", f"{api}/merge_requests", token,
        {"title": title, "source_branch": branch, "target_branch": run["base"],
         "description": body},
        timeout=timeout)
    if status != 201 or not isinstance(created, dict) or not created.get("iid"):
        outcome, detail = mutation_outcome(status, error, created)
        return done(outcome, f"could not create merge request: {detail}")
    iid = created["iid"]

    status, mr, error = request(
        "GET", f"{api}/merge_requests/{iid}", token, timeout=timeout)
    if status != 200 or not isinstance(mr, dict):
        outcome, detail = read_outcome(status, error)
        return done(outcome, f"created MR !{iid} but could not read it back: {detail}",
                    ref=created.get("web_url"))
    head_sha = mr.get("sha")
    if head_sha != candidate:
        return done("uncertain",
                    f"MR !{iid} head is {str(head_sha)[:12]}, not the reviewed "
                    f"head {candidate[:12]}", ref=mr.get("web_url"))
    return done("confirmed", f"MR !{iid} created with the reviewed head",
                ref=mr.get("web_url"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="GitLab delivery adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-mr")
    p.add_argument("--home", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--project",
                   help="group/name (default: task field gitlab_project, then origin remote)")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        print("error: GITLAB_TOKEN is not set", file=sys.stderr)
        return 2
    base_url = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
    run = read_run(args.home, args.task_id)
    project = resolve_project(run, args.project, base_url)
    if not project:
        print("error: cannot determine the GitLab project; pass --project or set "
              "gitlab_project in the task", file=sys.stderr)
        return 2
    data = create_mr(args.home, args.task_id, project, args.title, args.body,
                     base_url, args.timeout, token)
    write_report(args.out, data)
    print(f"gitlab {data['outcome']}" + (f" ref={data['ref']}" if data["ref"] else ""))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
