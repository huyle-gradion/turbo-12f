#!/usr/bin/env python3
"""GitHub delivery adapter (Phase 7).

Creates a pull request for a local-verified run's factory branch —
and never merges. The branch must already be pushed; the adapter
only reads and creates the PR, then reads it back.

Usage:
    python3 adapters/github.py create-pr --home <state-home> --task-id <id> \
        --out report.json [--repo owner/name] [--title T] [--body B] \
        [--api-base https://api.github.com] [--timeout 10]

The default body links the run's `source`: `Closes #N` when the task
came from an issue in this same repo, else a plain `Source:` link. The
task's title, criteria and manifest follow in code blocks, with issue
references broken, because an issue author may have written them. An
explicit --body is used as given.

Token from GITHUB_TOKEN. Exit 0 means a report was written (whatever
the outcome); non-zero means the adapter could not run at all.

Outcome rules:
- PR already exists with the reviewed head -> confirmed (no duplicate).
- PR exists with a different head -> rejected (the remote has another candidate).
- Branch not on the remote -> rejected (push it first).
- Create returns 403 -> denied. 422 -> rejected. 5xx/timeout -> uncertain.
- Read-back after create fails or disagrees -> uncertain (the PR may
  exist; the next run finds it instead of creating a duplicate).
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    break_issue_refs, manifest_text, mutation_outcome, read_outcome, read_run,
    report, request, source_lines, untrusted_block, write_report,
)


def parse_remote(url):
    """owner/repo from a GitHub remote URL, or None."""
    match = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+?)(?:\.git)?$", url or "")
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def resolve_repo(run, explicit):
    if explicit:
        return explicit
    if run.get("github_repo"):
        return run["github_repo"]
    try:
        url = subprocess.run(
            ["git", "-C", run["repo"], "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        url = ""
    return parse_remote(url)


def create_pr(home, task_id, repo, title, body, api_base, timeout, token):
    run = read_run(home, task_id)
    candidate = (run.get("candidate") or {}).get("commit")
    if not candidate:
        raise SystemExit("error: run has no staged candidate")
    branch = run["branch"]
    base = run["base"]
    manifest = manifest_text(home, task_id)
    if body is None:
        task = "\n".join([run["title"], "", "Acceptance criteria:",
                          *(f"- {c}" for c in run.get("acceptance_criteria") or [])])
        body = (
            f"Candidate: {candidate}\n"
            + "".join(f"{line}\n" for line in source_lines(run.get("source"), "github-issues", repo))
            + "\nThe task as written:\n\n" + untrusted_block(task)
            + (f"\nManifest:\n\n{untrusted_block(manifest)}" if manifest else "")
        )
    if title is None:
        # A squash merge makes the title a commit message on the default
        # branch, where a closing keyword in it closes the issue.
        title = break_issue_refs(run["title"])

    def fail(outcome, detail, ref=None):
        data = report("github", task_id, candidate, outcome, ref, detail)
        return data

    # 1. The branch must already be on the remote.
    status, _body, error = request(
        "GET", f"{api_base}/repos/{repo}/branches/{branch}", token, timeout=timeout)
    if status == 404:
        return fail("rejected", f"branch {branch!r} is not on the remote; push it first")
    if status != 200:
        outcome, detail = read_outcome(status, error)
        return fail(outcome, f"could not check remote branch: {detail}")

    # 2. A PR for this branch may already exist — find it, do not duplicate.
    status, listing, error = request(
        "GET", f"{api_base}/repos/{repo}/pulls?state=open&head={repo.split('/')[0]}:{branch}",
        token, timeout=timeout)
    if status != 200 or not isinstance(listing, list):
        outcome, detail = read_outcome(status, error)
        return fail(outcome, f"could not list pull requests: {detail}")
    if listing:
        number = listing[0].get("number")
        if not number:
            return fail("uncertain", "could not read the existing pull request number")
        status, pr, error = request(
            "GET", f"{api_base}/repos/{repo}/pulls/{number}", token, timeout=timeout)
        if status != 200 or not isinstance(pr, dict):
            outcome, detail = read_outcome(status, error)
            return fail(outcome, f"could not read back PR #{number}: {detail}")
        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha == candidate:
            return fail("confirmed", f"PR #{number} already exists with the reviewed head",
                        ref=pr.get("html_url"))
        return fail("rejected",
                    f"PR #{number} exists but its head is {str(head_sha)[:12]}, "
                    f"not the reviewed head {candidate[:12]}",
                    ref=pr.get("html_url"))

    # 3. Create the PR.
    status, created, error = request(
        "POST", f"{api_base}/repos/{repo}/pulls", token,
        {"title": title, "head": branch, "base": base, "body": body},
        timeout=timeout)
    if status != 201 or not isinstance(created, dict) or not created.get("number"):
        outcome, detail = mutation_outcome(status, error, created)
        return fail(outcome, f"could not create pull request: {detail}")
    number = created["number"]

    # 4. Read it back: a created PR whose head is not the reviewed
    #    commit is not a confirmed delivery.
    status, pr, error = request(
        "GET", f"{api_base}/repos/{repo}/pulls/{number}", token, timeout=timeout)
    if status != 200 or not isinstance(pr, dict):
        outcome, detail = read_outcome(status, error)
        return fail(outcome, f"created PR #{number} but could not read it back: {detail}",
                    ref=created.get("html_url"))
    head_sha = (pr.get("head") or {}).get("sha")
    if head_sha != candidate:
        return fail("uncertain",
                    f"PR #{number} head is {str(head_sha)[:12]}, not the reviewed "
                    f"head {candidate[:12]}", ref=pr.get("html_url"))
    return fail("confirmed", f"PR #{number} created with the reviewed head",
                ref=pr.get("html_url"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="GitHub delivery adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-pr")
    p.add_argument("--home", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--repo", help="owner/name (default: task field github_repo, then origin remote)")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--out", required=True)
    p.add_argument("--api-base", default="https://api.github.com")
    p.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    run = read_run(args.home, args.task_id)
    repo = resolve_repo(run, args.repo)
    if not repo:
        print("error: cannot determine owner/name; pass --repo or set github_repo in the task",
              file=sys.stderr)
        return 2
    data = create_pr(args.home, args.task_id, repo, args.title, args.body,
                     args.api_base.rstrip("/"), args.timeout, token)
    write_report(args.out, data)
    print(f"github {data['outcome']}" + (f" ref={data['ref']}" if data["ref"] else ""))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
