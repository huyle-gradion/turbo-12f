#!/usr/bin/env python3
"""GitHub issues intake adapter: open issues with a label become task files.

Usage:
    python3 adapters/github-issues.py intake --out-dir <queue dir> \
        [--repo-config .t12/project.json | --trunk-repo PATH --trunk BRANCH] \
        [--repo owner/name] [--label L] \
        [--home <state-home>] [--api-base https://api.github.com]

Token from GITHUB_TOKEN. See adapters/README.md for the issue format and
common.py for the rules every intake adapter follows.
"""

import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PER_PAGE, IntakeError, get_pages, intake_main, read_outcome, request  # noqa: E402

NAME = "github-issues"


def list_issues(args, repo, label, token):
    base = args.api_base.rstrip("/")

    def url_for(page):
        query = urlencode({"state": "open", "labels": label, "per_page": PER_PAGE, "page": page})
        return f"{base}/repos/{repo}/issues?{query}"

    items = get_pages(url_for, token)
    return [
        {"number": int(i["number"]), "title": i.get("title"), "body": i.get("body"),
         "url": i.get("html_url"),
         "sub_issues": int((i.get("sub_issues_summary") or {}).get("total") or 0),
         "blocked_by": blocked_by(base, repo, i, token)}
        for i in items
        # The issues endpoint also lists pull requests; they are not tasks.
        if "pull_request" not in i
        and label.lower() in {(lb.get("name") or "").lower() for lb in i.get("labels", [])
                              if isinstance(lb, dict)}
    ]


def blocked_by(base, repo, issue, token):
    """The issue's native "blocked by" issues as {repo, number, state};
    fetched only when the issue says it has any."""
    if not (issue.get("issue_dependencies_summary") or {}).get("total_blocked_by"):
        return []
    url = f"{base}/repos/{repo}/issues/{issue['number']}/dependencies/blocked_by?per_page={PER_PAGE}"
    status, body, error = request("GET", url, token)
    if status != 200 or not isinstance(body, list):
        raise IntakeError(f"could not read what blocks #{issue['number']}: "
                          f"{read_outcome(status, error)[1]}")
    return [{"repo": "/".join(str(b.get("repository_url", "")).split("/")[-2:]),
             "number": int(b["number"]),
             "state": "open" if b.get("state") == "open"
             else "completed" if b.get("state_reason") == "completed" else "not_planned"}
            for b in body]


def main(argv=None):
    return intake_main(
        argv, NAME, "gh", "GITHUB_TOKEN", list_issues,
        lambda p: p.add_argument("--api-base", default="https://api.github.com"),
        depends_text=False)


if __name__ == "__main__":
    raise SystemExit(main())
