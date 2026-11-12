"""Shared helpers for the delivery adapters (Phase 7).

Adapters are separate programs: they run outside the factory core,
hold the host's credentials, and talk to the network. The core never
imports this module. Adapters are stdlib-only.

Outcome rules (from the original Turbo's integration lessons):
- A failed READ is always `uncertain`. The adapter could not
  establish the remote state — a 401/403 or a 5xx on a read is
  not a denial and not a rejection.
- A failed MUTATION is `denied` on 403 (permission/policy),
  `rejected` on 404/422 and other definite 4xx (the remote said
  no), and `uncertain` on 5xx, timeouts, and transport errors
  (the mutation may or may not have landed).
- An adapter exits 0 when it wrote a report, whatever the outcome.
  A non-zero exit means the adapter itself could not run (missing
  token, missing run record, bad arguments) and no report exists.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request


def read_run(home, task_id):
    path = os.path.join(home, "runs", f"{task_id}.json")
    if not os.path.isfile(path):
        raise SystemExit(f"error: no run record for {task_id} in {home}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def source_lines(source, issue_adapter, repo):
    """Lines for a change-request body that link the task's origin. The
    closing keyword is only for an issue in the same repo as the change
    request; an issue elsewhere gets a plain link, never a close."""
    if not source:
        return []
    lines = []
    match = re.fullmatch(r"(.+)#(\d+)", source["ref"])
    if (source["adapter"] == issue_adapter and match
            and match.group(1).lower() == repo.lower()):
        lines.append(f"Closes #{match.group(2)}")
    lines.append(f"Source: {source.get('url') or source['ref']}")
    return lines


ISSUE_REF_RE = re.compile(r"#(?=\d)")


def break_issue_refs(text):
    """Text with no `#N` issue reference left: a WORD JOINER follows each
    `#` before a digit, so a forge neither links nor closes it."""
    return ISSUE_REF_RE.sub("#\u2060", text)


def untrusted_block(text):
    """Markdown that shows text as written and can do nothing else: no
    closing keyword, no HTML comment that hides what follows. The fence
    is longer than any backtick run in the text, so no line ends it."""
    text = break_issue_refs(text.rstrip("\n"))
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}\n{fence}\n"


def manifest_text(home, task_id):
    path = os.path.join(home, "evidence", task_id, "manifest.txt")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def request(method, url, token, payload=None, timeout=10):
    """One HTTP call. Returns (status, body, error):
    status is None on a transport failure (timeout, DNS, refused);
    body is parsed JSON when the response has any; error describes a
    transport failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _parse_json(raw), None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, _parse_json(raw), None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"transport: {exc}"


def _parse_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


def read_outcome(status, error):
    """Outcome for a failed read: always uncertain. The remote state
    is unknown, so nothing may be concluded from the failure."""
    if error is not None:
        return "uncertain", error
    if status in (401, 403):
        return "uncertain", f"read returned {status} (could not establish remote state)"
    if status is not None and status >= 500:
        return "uncertain", f"read returned {status}"
    return "uncertain", f"read returned {status}"


def mutation_outcome(status, error, body=None):
    """Outcome for a failed mutation: denied only when the remote
    refused permission; rejected when it definitively said no;
    uncertain when the effect is unknown."""
    if error is not None:
        return "uncertain", error
    if status == 403:
        return "denied", _message(body, f"mutation forbidden ({status})")
    if status == 404:
        return "rejected", _message(body, f"mutation target not found ({status})")
    if status == 422:
        return "rejected", _message(body, f"mutation rejected ({status})")
    if status is not None and status >= 500:
        return "uncertain", f"mutation returned {status} (effect unknown)"
    if status is not None and 400 <= status < 500:
        return "rejected", _message(body, f"mutation rejected ({status})")
    return "uncertain", f"mutation returned {status}"


def _message(body, default):
    if isinstance(body, dict):
        for key in ("message", "errors"):
            value = body.get(key)
            if value:
                return str(value)[:300]
    return default


def report(adapter, task_id, head, outcome, ref, detail):
    return {
        "adapter": adapter,
        "task_id": task_id,
        "head": head,
        "outcome": outcome,
        "ref": ref,
        "detail": detail,
        "checked_at": time.time(),
    }


def write_report(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


# --- Intake (issue tracker -> task files) -------------------------------
#
# An intake adapter turns issues into task files in the queue directory
# `bin/t12-queue` reads. The issue author is untrusted: the adapter
# builds each task from a fixed list of fields and never copies a key
# out of an issue, so no profile-controlled setting (gates, reviews,
# sandbox, budgets, roles, paths) can arrive through an issue. Whatever
# the core would refuse at `prepare` is skipped here, with the reason.
# Intake writes no outcome report, so a failed listing writes nothing
# and exits non-zero; a half-written queue must not look complete.
# Only a complete listing withdraws a task, because only it shows that
# an issue lost its label.

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
CRITERIA_HEADING_RE = re.compile(r"^acceptance criteria:?$", re.I)
ITEM_RE = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
LABELLED_RE = re.compile(r"^\s*(touches|depends[ _-]?on)\s*:\s*(.*?)\s*$", re.I)
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
COMMENT_RE = re.compile(r"<!--.*?-->")
MAX_CRITERIA = 50
MAX_TEXT = 1000
PER_PAGE = 100
MAX_PAGES = 20


class IntakeError(Exception):
    """The adapter could not establish the issue list; nothing is written."""


def _visible_lines(body):
    """Lines of the body that a forge renders as text: outside code
    fences and HTML comments. The maintainer who applies the label
    reads only these. An unclosed `<!--` hides the rest of the body."""
    fenced = in_comment = False
    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fenced:
            fenced = not FENCE_RE.match(line)
            continue
        if in_comment:
            end = line.find("-->")
            if end < 0:
                continue
            line, in_comment = line[end + 3:], False
        elif FENCE_RE.match(line):
            fenced = True
            continue
        line = COMMENT_RE.sub("", line)
        start = line.find("<!--")
        if start >= 0:
            line, in_comment = line[:start], True
        yield line


def parse_body(body, id_prefix):
    """(fields, problem) from an issue body. Fields hold only
    `acceptance_criteria` and, when labelled, `touches` and
    `depends_on`; problem is why the issue cannot become a task."""
    criteria, in_section, seen = [], False, False
    labelled = {}
    for line in _visible_lines(body):
        heading = HEADING_RE.match(line)
        if heading:
            in_section = not seen and bool(CRITERIA_HEADING_RE.match(heading.group(1)))
            seen = seen or in_section
            continue
        item = ITEM_RE.match(line)
        if in_section and item:
            criteria.append(item.group(1))
            continue
        label = LABELLED_RE.match(line)
        if label:
            key = "touches" if label.group(1).lower() == "touches" else "depends_on"
            labelled.setdefault(key, []).extend(
                v for v in re.split(r"[,\s]+", label.group(2)) if v)
    if not criteria:
        return None, 'no "Acceptance criteria" heading with a "- " list under it'
    if len(criteria) > MAX_CRITERIA or any(len(c) > MAX_TEXT for c in criteria):
        return None, f"more than {MAX_CRITERIA} criteria or one longer than {MAX_TEXT} characters"
    fields = {"acceptance_criteria": criteria}
    if "touches" in labelled:
        touches = [os.path.normpath(t) for t in labelled["touches"]]
        bad = [t for t in labelled["touches"] if not _worktree_path(t)]
        if bad or not touches:
            return None, f"Touches must be relative paths inside the repo, got {bad or 'none'}"
        fields["touches"] = sorted(set(touches))
    if "depends_on" in labelled:
        ids = [f"{id_prefix}-{d[1:]}" if re.fullmatch(r"#\d+", d) else d
               for d in labelled["depends_on"]]
        bad = [d for d in ids if not TASK_ID_RE.match(d)]
        if bad or not ids:
            return None, f"Depends on must be task ids or #numbers, got {bad or 'none'}"
        fields["depends_on"] = sorted(set(ids))
    return fields, None


def _worktree_path(path):
    if os.path.isabs(path):
        return False
    return os.path.normpath(path).split(os.sep)[0] not in ("..", ".", ".git")


def issue_to_task(issue, adapter, repo, id_prefix):
    """(task, problem) for one normalised issue: number, title, body, url."""
    task_id = f"{id_prefix}-{issue['number']}"
    fields, problem = parse_body(issue["body"], id_prefix)
    title = " ".join(str(issue["title"] or "").split())[:200]
    if not problem and not title:
        problem = "the issue has no title"
    if not problem and task_id in fields.get("depends_on", []):
        problem = "the issue depends on itself"
    if problem:
        return task_id, None, problem
    task = {
        "id": task_id,
        "title": title,
        "acceptance_criteria": fields["acceptance_criteria"],
        "source": {"adapter": adapter, "ref": f"{repo}#{issue['number']}"},
    }
    if re.fullmatch(r"https?://\S+", issue.get("url") or ""):
        task["source"]["url"] = issue["url"]
    for key in ("touches", "depends_on"):
        if key in fields:
            task[key] = fields[key]
    return task_id, task, None


def get_pages(url_for_page, token):
    """Every item of a paged list endpoint, or IntakeError. A page that
    fails after earlier pages succeeded fails the whole listing."""
    items = []
    for page in range(1, MAX_PAGES + 1):
        status, body, error = request("GET", url_for_page(page), token)
        if status != 200 or not isinstance(body, list):
            raise IntakeError(f"could not list issues: {read_outcome(status, error)[1]}")
        items.extend(body)
        if len(body) < PER_PAGE:
            return items
    raise IntakeError(f"more than {MAX_PAGES * PER_PAGE} issues match; narrow the label")


def _has_run(home, task_id):
    return bool(home) and os.path.exists(os.path.join(home, "runs", f"{task_id}.json"))


def _render(task):
    return json.dumps(task, indent=2, sort_keys=True) + "\n"


def _source_of(text):
    """(adapter, ref) of a task file's text, or None."""
    try:
        source = json.loads(text).get("source") or {}
        return source.get("adapter"), source.get("ref")
    except (ValueError, AttributeError):
        return None


def _write_if_changed(out_dir, task, adapter):
    """(verdict, detail). Overwrites only a file this adapter wrote for
    the same issue: ids repeat across repos, so the adapter alone is
    not enough."""
    path = os.path.join(out_dir, f"{task['id']}.json")
    text = _render(task)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        mine = (adapter, task["source"]["ref"])
        owner = _source_of(current)
        if owner != mine:
            found = f"{owner[0]} {owner[1]}" if owner and all(owner) else "no intake source"
            return "skipped", (f"{path} exists and holds {found}, "
                               f"not {mine[0]} {mine[1]}")
        if current == text:
            return "unchanged", path
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{task['id']}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return "wrote", path


def sync(issues, adapter, repo, id_prefix, out_dir, home):
    """Write one task file per usable issue and withdraw the tasks of
    issues no longer listed; one printed line each."""
    os.makedirs(out_dir, exist_ok=True)
    for issue in sorted(issues, key=lambda i: i["number"]):
        task_id, task, problem = issue_to_task(issue, adapter, repo, id_prefix)
        if problem:
            print(f"skipped {task_id}: {problem}")
        elif _has_run(home, task_id):
            print(f"has-run {task_id}: left alone, a run exists in {home}")
        else:
            verdict, detail = _write_if_changed(out_dir, task, adapter)
            print(f"{verdict} {task_id}: {detail}")
    _withdraw({f"{repo}#{i['number']}" for i in issues}, adapter, repo, out_dir, home)


def _withdraw(listed, adapter, repo, out_dir, home):
    """Delete this repo's queued tasks whose issue the complete listing
    no longer holds: the label was removed or the issue closed."""
    for entry in sorted(os.scandir(out_dir), key=lambda e: e.name):
        if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
            continue
        task_id = entry.name[:-len(".json")]
        try:
            with open(entry.path, encoding="utf-8") as fh:
                task = json.load(fh)
            source = task.get("source") or {}
            owner, ref = source.get("adapter"), str(source.get("ref"))
        except (OSError, ValueError, AttributeError):
            continue
        if (owner != adapter or task.get("id") != task_id
                or ref.rpartition("#")[0] != repo or ref in listed):
            continue
        if _has_run(home, task_id):
            print(f"has-run {task_id}: no longer listed, left alone, a run exists in {home}")
            continue
        os.unlink(entry.path)
        print(f"withdrawn {task_id}: {entry.path}")


def intake_main(argv, name, id_prefix, token_env, list_issues, add_arguments=None):
    """Shared `intake` command. list_issues(args, repo, label, token)
    returns normalised issues (number, title, body, url) or raises
    IntakeError. Exit 0: every issue was handled. 1: the listing
    failed, nothing written. 2: the adapter could not run."""
    parser = argparse.ArgumentParser(description=f"{name} intake adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("intake")
    p.add_argument("--repo-config", help="path to .t12/project.json; its `intake` key sets defaults")
    p.add_argument("--out-dir", required=True, help="queue directory bin/t12-queue reads")
    p.add_argument("--home", help="state home; a task with a run there is left alone")
    p.add_argument("--repo", help="owner/name, overriding the profile")
    p.add_argument("--label", help="only issues with this label, overriding the profile")
    if add_arguments:
        add_arguments(p)
    args = parser.parse_args(argv)

    token = os.environ.get(token_env)
    if not token:
        print(f"error: {token_env} is not set", file=sys.stderr)
        return 2
    try:
        profile = _intake_profile(args.repo_config, name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    repo = args.repo or profile.get("repo")
    label = args.label or profile.get("label")
    if not repo or not label:
        # Anyone can file an issue; the label is what a maintainer
        # applies to say "the factory may work on this".
        print("error: give --repo and --label, or an `intake` key in --repo-config; "
              "without a label every open issue would become a task", file=sys.stderr)
        return 2
    try:
        issues = list_issues(args, repo, label, token)
    except IntakeError as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return 1
    sync(issues, name, repo, id_prefix, os.path.abspath(args.out_dir), args.home)
    return 0


def _intake_profile(path, name):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            intake = json.load(fh).get("intake") or {}
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(intake, dict) or intake.get("adapter") not in (None, name):
        raise ValueError(f"{path} does not name {name!r} as its intake adapter")
    return intake
