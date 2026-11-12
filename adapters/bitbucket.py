#!/usr/bin/env python3
"""Bitbucket Cloud delivery adapter (Phase 7).

Creates a pull request for a local-verified run's factory branch —
and never merges. The branch must already be pushed; the adapter
only reads and creates the PR, then reads it back.

Usage:
    python3 adapters/bitbucket.py create-pr --home <state-home> --task-id <id> \
        --out report.json [--repo workspace/slug] [--title T] [--body B] \
        [--timeout 10]

Token (bearer) from BITBUCKET_TOKEN, API base from BITBUCKET_URL
(default https://api.bitbucket.org/2.0). Exit 0 means a report was
written (whatever the outcome); non-zero means the adapter could not
run at all.

Outcome rules:
- PR already open for the branch with the reviewed head -> confirmed
  (no duplicate).
- PR exists with a different head -> rejected.
- Branch not on the remote -> rejected (push it first).
- Create returns 403 -> denied. Other definite 4xx -> rejected.
  5xx/timeout -> uncertain.
- Read-back after create fails or disagrees -> uncertain (the PR may
  exist; the next run finds it instead of creating a duplicate).
- Any failed read -> uncertain, never denied or rejected.

Bitbucket returns a 12-character abbreviated source commit hash, so
the head is compared as a prefix of the full candidate SHA. A hash
shorter than MIN_HASH is too ambiguous to compare: the remote state
is unknown, so the outcome is uncertain.
"""

import argparse
import os
import sys
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    break_issue_refs, change_request_body, missing_branch_outcome, mutation_outcome, origin_url, parse_remote,
    read_outcome, read_run, report, request, write_report,
)

MIN_HASH = 12


def resolve_repo(run, explicit):
    if explicit:
        return explicit
    if run.get("bitbucket_repo"):
        return run["bitbucket_repo"]
    remote = parse_remote(origin_url(run))
    if remote and remote[0] == "bitbucket.org" and remote[1].count("/") == 1:
        return remote[1]
    return None


def compare_head(actual, candidate):
    """'match', 'mismatch' or 'unknown' for a possibly abbreviated hash."""
    if not isinstance(actual, str) or len(actual) < MIN_HASH:
        return "unknown"
    return "match" if candidate.lower().startswith(actual.lower()) else "mismatch"


def create_pr(home, task_id, repo, title, body, api_base, timeout, token):
    run = read_run(home, task_id)
    candidate = (run.get("candidate") or {}).get("commit")
    if not candidate:
        raise SystemExit("error: run has no staged candidate")
    branch = run["branch"]
    if body is None:
        body = change_request_body(home, task_id, run, candidate, None, repo)
    if title is None:
        title = break_issue_refs(run["title"])
    api = f"{api_base}/repositories/{repo}"

    def done(outcome, detail, ref=None):
        return report("bitbucket", task_id, candidate, outcome, ref, detail)

    def ref_of(pr):
        return ((pr.get("links") or {}).get("html") or {}).get("href")

    def head_of(pr):
        return ((pr.get("source") or {}).get("commit") or {}).get("hash")

    def describe(actual):
        return str(actual)[:12] if actual else "missing"

    status, branch_ref, error = request(
        "GET", f"{api}/refs/branches/{quote(branch, safe='/')}", token, timeout=timeout)
    if status == 404:
        repo_status, _, _ = request("GET", api, token, timeout=timeout)
        return done(*missing_branch_outcome(branch, repo_status))
    if status != 200:
        outcome, detail = read_outcome(status, error)
        return done(outcome, f"could not check remote branch: {detail}")
    # Pull requests report an abbreviated hash; the branch reports the
    # full one, so the full compare happens here.
    tip = ((branch_ref or {}).get("target") or {}).get("hash") if isinstance(branch_ref, dict) else None
    if not isinstance(tip, str) or len(tip) != len(candidate):
        return done("uncertain", f"could not read the full head of branch {branch!r}")
    if tip.lower() != candidate.lower():
        return done("rejected", f"branch {branch!r} is at {tip[:12]}, not the reviewed "
                    f"head {candidate[:12]}; push the reviewed head")

    query = urlencode({"state": "OPEN", "q": f'source.branch.name="{branch}" AND '
                                             f'destination.branch.name="{run["base"]}"'})
    status, listing, error = request(
        "GET", f"{api}/pullrequests?{query}", token, timeout=timeout)
    if status != 200 or not isinstance(listing, dict) or not isinstance(listing.get("values"), list):
        outcome, detail = read_outcome(status, error)
        return done(outcome, f"could not list pull requests: {detail}")
    if listing["values"]:
        pr_id = listing["values"][0].get("id")
        if not pr_id:
            return done("uncertain", "could not read the existing pull request id")
        status, pr, error = request(
            "GET", f"{api}/pullrequests/{pr_id}", token, timeout=timeout)
        if status != 200 or not isinstance(pr, dict):
            outcome, detail = read_outcome(status, error)
            return done(outcome, f"could not read back PR #{pr_id}: {detail}")
        actual = head_of(pr)
        verdict = compare_head(actual, candidate)
        target = ((pr.get("destination") or {}).get("branch") or {}).get("name")
        if verdict == "match" and target != run["base"]:
            return done("rejected", f"PR #{pr_id} has the reviewed head but targets "
                        f"{target!r}, not {run['base']!r}", ref=ref_of(pr))
        if verdict == "match":
            return done("confirmed", f"PR #{pr_id} already exists with the reviewed head",
                        ref=ref_of(pr))
        if verdict == "unknown":
            return done("uncertain",
                        f"PR #{pr_id} head hash {describe(actual)!r} is too short to compare "
                        f"with the reviewed head {candidate[:12]}", ref=ref_of(pr))
        return done("rejected",
                    f"PR #{pr_id} exists but its head is {describe(actual)}, "
                    f"not the reviewed head {candidate[:12]}", ref=ref_of(pr))

    status, created, error = request(
        "POST", f"{api}/pullrequests", token,
        {"title": title, "description": body,
         "source": {"branch": {"name": branch}},
         "destination": {"branch": {"name": run["base"]}}},
        timeout=timeout)
    if status != 201 or not isinstance(created, dict) or not created.get("id"):
        outcome, detail = mutation_outcome(status, error, created)
        return done(outcome, f"could not create pull request: {detail}")
    pr_id = created["id"]

    status, pr, error = request(
        "GET", f"{api}/pullrequests/{pr_id}", token, timeout=timeout)
    if status != 200 or not isinstance(pr, dict):
        outcome, detail = read_outcome(status, error)
        return done(outcome, f"created PR #{pr_id} but could not read it back: {detail}",
                    ref=ref_of(created))
    actual = head_of(pr)
    if compare_head(actual, candidate) != "match":
        return done("uncertain",
                    f"PR #{pr_id} head is {describe(actual)}, not the reviewed "
                    f"head {candidate[:12]}", ref=ref_of(pr))
    return done("confirmed", f"PR #{pr_id} created with the reviewed head", ref=ref_of(pr))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bitbucket Cloud delivery adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-pr")
    p.add_argument("--home", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--repo",
                   help="workspace/slug (default: task field bitbucket_repo, then origin remote)")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    token = os.environ.get("BITBUCKET_TOKEN")
    if not token:
        print("error: BITBUCKET_TOKEN is not set", file=sys.stderr)
        return 2
    api_base = os.environ.get("BITBUCKET_URL", "https://api.bitbucket.org/2.0").rstrip("/")
    run = read_run(args.home, args.task_id)
    repo = resolve_repo(run, args.repo)
    if not repo:
        print("error: cannot determine workspace/slug; pass --repo or set bitbucket_repo "
              "in the task", file=sys.stderr)
        return 2
    data = create_pr(args.home, args.task_id, repo, args.title, args.body,
                     api_base, args.timeout, token)
    write_report(args.out, data)
    print(f"bitbucket {data['outcome']}" + (f" ref={data['ref']}" if data["ref"] else ""))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
