#!/usr/bin/env python3
"""GitLab issues intake adapter: opened issues with a label become task files.

Usage:
    python3 adapters/gitlab-issues.py intake --out-dir <queue dir> \
        [--repo-config .t12/project.json | --trunk-repo PATH --trunk BRANCH] \
        [--repo group/project] [--label L] \
        [--home <state-home>]

Token from GITLAB_TOKEN; the instance from GITLAB_URL (default
https://gitlab.com). See adapters/README.md for the issue format and
common.py for the rules every intake adapter follows.
"""

import os
import sys
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PER_PAGE, get_pages, intake_main  # noqa: E402

NAME = "gitlab-issues"


def list_issues(args, repo, label, token):
    base = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
    project = quote(repo, safe="")

    def url_for(page):
        query = urlencode({"state": "opened", "labels": label, "per_page": PER_PAGE, "page": page})
        return f"{base}/api/v4/projects/{project}/issues?{query}"

    items = get_pages(url_for, token)
    return [
        {"number": int(i["iid"]), "title": i.get("title"), "body": i.get("description"),
         "url": i.get("web_url")}
        for i in items
        if label.lower() in {str(lb).lower() for lb in i.get("labels", [])}
    ]


def main(argv=None):
    return intake_main(argv, NAME, "gl", "GITLAB_TOKEN", list_issues)


if __name__ == "__main__":
    raise SystemExit(main())
