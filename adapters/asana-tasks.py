#!/usr/bin/env python3
"""Asana tasks intake adapter: open tagged tasks become task files.

Usage:
    python3 adapters/asana-tasks.py intake --out-dir <queue dir> \
        [--repo-config .t12/project.json | --trunk-repo PATH --trunk BRANCH] \
        [--project GID] [--tag NAME] \
        [--home <state-home>] [--api-base https://app.asana.com/api/1.0]

Token from ASANA_TOKEN. The approval marker is a tag on the task; the
criteria are an "Acceptance criteria" list in its description (rich text
is read as the equivalent markdown). Each approved open sub-task is its
own queued task; a task with open sub-tasks is a parent and never runs. Task ids are `as-<gid>`.
See common.py for the rules every intake adapter follows.
"""

import os
import sys
from html.parser import HTMLParser
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

NAME = "asana-tasks"
PAGE = 20
FIELDS = "name,notes,html_notes,completed,permalink_url,tags.name"
BLOCKS = {"p", "div", "ul", "ol", "br", "pre", "blockquote", "body"}


class _Markdown(HTMLParser):
    """Asana rich text as the markdown a GitHub issue would hold."""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.parts.append("\n## ")
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_comment(self, data):
        self.parts.append("\n<!--\n")


def description(task):
    html = task.get("html_notes")
    if not html:
        return task.get("notes") or ""
    parser = _Markdown()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def _list(url, token):
    items, offset = [], None
    for _ in range(common.MAX_PAGES * 10):
        query = {"opt_fields": FIELDS, "limit": PAGE}
        if offset:
            query["offset"] = offset
        status, body, error = common.request("GET", f"{url}?{urlencode(query)}", token)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise common.IntakeError(
                f"could not list Asana tasks: {common.read_outcome(status, error)[1]}")
        items.extend(body["data"])
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            return items
    raise common.IntakeError("the Asana listing never ended")


def list_issues(args, project, tag, token):
    base = args.api_base.rstrip("/")
    issues = []

    def approved(tasks):
        return [t for t in tasks if not t.get("completed") and tag.lower() in
                {(x.get("name") or "").lower() for x in t.get("tags") or [] if isinstance(x, dict)}]

    def collect(tasks):
        """Normalised issues for approved TASKS and their approved
        sub-tasks. A task with open sub-tasks is a parent, and intake
        refuses it; its sub-tasks run on their own."""
        for task in sorted(approved(tasks), key=lambda t: int(t["gid"])):
            children = _list(f"{base}/tasks/{task['gid']}/subtasks", token)
            issues.append({"number": int(task["gid"]), "title": task.get("name"),
                           "body": description(task), "url": task.get("permalink_url"),
                           "sub_issues": sum(1 for c in children if not c.get("completed"))})
            collect(children)

    collect(_list(f"{base}/projects/{project}/tasks", token))
    return issues


def main(argv=None):
    return common.intake_main(
        argv, NAME, "as", "ASANA_TOKEN", list_issues,
        lambda p: p.add_argument("--api-base", default="https://app.asana.com/api/1.0"),
        scope=("project", "tag"))


if __name__ == "__main__":
    raise SystemExit(main())
