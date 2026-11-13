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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
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
CLOSING_KEYWORD_RE = re.compile(r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b", re.I)


def break_issue_refs(text):
    """Text that can neither link nor close an issue: a WORD JOINER
    follows each `#` before a digit and splits every closing keyword.
    Splitting the keyword covers every reference form (`#N`, `GH-N`,
    an issue URL, `owner/repo#N`) at once."""
    text = CLOSING_KEYWORD_RE.sub(lambda m: m.group(1)[0] + "\u2060" + m.group(1)[1:], text)
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


def change_request_body(home, task_id, run, candidate, issue_adapter, repo):
    """The default change-request body. The task's title, criteria and
    manifest may come from an issue author, so they go in as inert
    text; only the factory's own lines can link or close an issue."""
    manifest = manifest_text(home, task_id)
    task = "\n".join([run["title"], "", "Acceptance criteria:",
                      *(f"- {c}" for c in run.get("acceptance_criteria") or [])])
    return (
        f"Candidate: {candidate}\n"
        + "".join(f"{line}\n" for line in source_lines(run.get("source"), issue_adapter, repo))
        + "\nThe task as written:\n\n" + untrusted_block(task)
        + (f"\nManifest:\n\n{untrusted_block(manifest)}" if manifest else "")
    )


def origin_url(run):
    try:
        return subprocess.run(
            ["git", "-C", run["repo"], "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def parse_remote(url):
    """(host, path) from an scp-style or URL git remote, or None.
    The path has no leading slash and no .git suffix."""
    url = url or ""
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$", url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https", "ssh", "git") or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return (host.lower(), path) if path else None


def missing_branch_outcome(branch, project_status):
    """The outcome of a 404 on the branch read. Forges answer 404 for a
    project the token cannot see too, so the branch is only definitely
    missing once the project itself reads back."""
    if project_status == 200:
        return "rejected", f"branch {branch!r} is not on the remote; push it first"
    return "uncertain", (f"branch {branch!r} was not found and the project could not be "
                         f"read (status {project_status}); check the token and project name")


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
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("message"):
            return str(nested["message"])[:300]
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
PLAIN_HEADING_RE = re.compile(r"^\s{0,3}(acceptance criteria):?\s*$", re.I)
CRITERIA_HEADING_RE = re.compile(r"^acceptance criteria\b", re.I)
OPEN_HEADING_RE = re.compile(r"^open questions?\b", re.I)
OPEN_LINE_RE = re.compile(r"^[\s_*]*open questions?\s*:(.*)$", re.I)
ITEM_RE = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
LABELLED_RE = re.compile(r"^\s*(touches|depends[ _-]?on)\s*:\s*(.*?)\s*$", re.I)
MAX_CRITERIA = 10
WARN_CRITERIA = 7
MAX_TEXT = 1000
PER_PAGE = 100
MAX_PAGES = 20
STANDARD = "docs/spec-standard.md"


class IntakeError(Exception):
    """The adapter could not establish the issue list; nothing is written."""


def _is_none(text):
    return text.strip(" \t_*.").lower() in ("", "none")


def spec_check(body, id_prefix, depends_text=True):
    """(fields, refusals, warnings) for an issue body, by the rules in
    docs/spec-standard.md that the body alone can show. Fields hold only
    `acceptance_criteria`, `touches` and, when labelled, `depends_on`.
    DEPENDS_TEXT is false on a forge with native dependencies, where a
    `Depends on:` line is refused so the order lives in one place."""
    # Intake cannot tell exactly what a forge hides: comment and fence
    # rules differ between parsers, and a wrong guess either reads text
    # the maintainer never saw or drops a scope line they did see. So an
    # issue with a comment is refused, and every other line is read,
    # fenced or not; visible code can only narrow a task.
    if "<!--" in (body or ""):
        return None, ["the body contains an HTML comment, so intake cannot be sure what the page shows"], []
    criteria, section, seen = [], None, False
    labelled, open_questions = {}, []
    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        heading = HEADING_RE.match(line) or PLAIN_HEADING_RE.match(line)
        if heading:
            title = heading.group(1)
            if not seen and CRITERIA_HEADING_RE.match(title):
                section, seen = "criteria", True
            else:
                section = "open" if OPEN_HEADING_RE.match(title) else None
            continue
        item = ITEM_RE.match(line)
        if section == "criteria" and item:
            criteria.append(item.group(1))
            continue
        marker = OPEN_LINE_RE.match(line)
        if marker:
            if not _is_none(marker.group(1)):
                open_questions.append(line.strip())
            continue
        if section == "open" and not _is_none(line):
            open_questions.append(line.strip())
            continue
        label = LABELLED_RE.match(line)
        if label:
            key = "touches" if label.group(1).lower() == "touches" else "depends_on"
            labelled.setdefault(key, []).extend(
                v for v in re.split(r"[,\s]+", label.group(2)) if v)
    refusals, warnings = [], []
    if not criteria:
        refusals.append('no "Acceptance criteria" heading with a "- " list under it')
    elif len(criteria) > MAX_CRITERIA:
        refusals.append(f"{len(criteria)} acceptance criteria, more than {MAX_CRITERIA}: "
                        f"split it into sub-issues ({STANDARD})")
    elif len(criteria) >= WARN_CRITERIA:
        warnings.append(f"{len(criteria)} acceptance criteria; consider a split ({STANDARD})")
    if any(len(c) > MAX_TEXT for c in criteria):
        refusals.append(f"an acceptance criterion is longer than {MAX_TEXT} characters")
    if open_questions:
        refusals.append(f"open questions are left: {open_questions[0][:200]!r}; "
                        "decide them and record the choice under Decisions")
    fields = {"acceptance_criteria": criteria}
    touches = labelled.get("touches", [])
    bad = [t for t in touches if not _worktree_path(t)]
    if bad or not touches:
        refusals.append(f"Touches must list relative paths inside the repo, got {bad or 'none'}")
    else:
        fields["touches"] = sorted({os.path.normpath(t) for t in touches})
    if "depends_on" in labelled and not depends_text:
        refusals.append("a Depends on: line is refused here; use the forge's native "
                        "\"blocked by\" relation instead")
    elif "depends_on" in labelled:
        ids = [f"{id_prefix}-{d[1:]}" if re.fullmatch(r"#\d+", d) else d
               for d in labelled["depends_on"]]
        bad = [d for d in ids if not TASK_ID_RE.match(d)]
        if bad or not ids:
            refusals.append(f"Depends on must be task ids or #numbers, got {bad or 'none'}")
        else:
            fields["depends_on"] = sorted(set(ids))
    return (None if refusals else fields), refusals, warnings


def _worktree_path(path):
    if os.path.isabs(path):
        return False
    return os.path.normpath(path).split(os.sep)[0] not in ("..", ".", ".git")


def relation_refusals(issue, repo, id_prefix):
    """(depends_on ids, refusals) from an issue's native relations.
    `sub_issues` counts its children: a parent never runs, only its
    sub-issues do. `blocked_by` lists {repo, number, state} with state
    open, completed or not_planned; a completed blocker has landed and
    adds no wait."""
    refusals, ids = [], []
    if issue.get("sub_issues"):
        refusals.append(f"it has {issue['sub_issues']} sub-issue(s); a parent never runs, "
                        "so remove its label and label the sub-issues")
    for blocker in issue.get("blocked_by") or []:
        name = f"{blocker['repo']}#{blocker['number']}"
        if blocker["repo"].lower() != repo.lower():
            refusals.append(f"it is blocked by {name} in another repository, which the queue cannot see")
        elif blocker["state"] == "open":
            ids.append(f"{id_prefix}-{blocker['number']}")
        elif blocker["state"] != "completed":
            refusals.append(f"it is blocked by {name}, which was closed without landing")
    return sorted(set(ids)), refusals


def issue_to_task(issue, adapter, repo, id_prefix, depends_text=True):
    """(task_id, task, problem, warnings) for one normalised issue:
    number, title, body, url, and optionally sub_issues and blocked_by."""
    task_id = f"{id_prefix}-{issue['number']}"
    fields, refusals, warnings = spec_check(issue["body"], id_prefix, depends_text)
    native, more = relation_refusals(issue, repo, id_prefix)
    refusals += more
    title = " ".join(str(issue["title"] or "").split())[:200]
    if not title:
        refusals.append("the issue has no title")
    depends = sorted(set((fields or {}).get("depends_on", [])) | set(native))
    if task_id in depends:
        refusals.append("the issue depends on itself")
    if refusals:
        return task_id, None, "; ".join(refusals), warnings
    task = {
        "id": task_id,
        "title": title,
        "acceptance_criteria": fields["acceptance_criteria"],
        "source": {"adapter": adapter, "ref": f"{repo}#{issue['number']}"},
        "touches": fields["touches"],
    }
    if re.fullmatch(r"https?://\S+", issue.get("url") or ""):
        task["source"]["url"] = issue["url"]
    if depends:
        task["depends_on"] = depends
    return task_id, task, None, warnings


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


def sync(issues, adapter, repo, id_prefix, out_dir, home, depends_text=True):
    """Write one task file per usable issue and withdraw the tasks of
    issues no longer listed; one printed line each."""
    os.makedirs(out_dir, exist_ok=True)
    for issue in sorted(issues, key=lambda i: i["number"]):
        task_id, task, problem, warnings = issue_to_task(
            issue, adapter, repo, id_prefix, depends_text)
        for warning in warnings:
            print(f"warning {task_id}: {warning}")
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


def intake_main(argv, name, id_prefix, token_env, list_issues, add_arguments=None,
                scope=("repo", "label"), depends_text=True):
    """Shared `intake` command. list_issues(args, repo, label, token)
    returns normalised issues (number, title, body, url, and optionally
    sub_issues and blocked_by) or raises IntakeError. DEPENDS_TEXT is
    false for a forge whose native dependencies replace the text line. `scope` names the two
    settings (flag and profile key) that select what to list. Exit 0: every issue was handled. 1: the listing
    failed, nothing written. 2: the adapter could not run."""
    parser = argparse.ArgumentParser(description=f"{name} intake adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("intake")
    p.add_argument("--repo-config", help="path to .t12/project.json; its `intake` key sets defaults")
    p.add_argument("--trunk-repo", help="git checkout whose --trunk branch carries .t12/project.json")
    p.add_argument("--trunk", help="branch to read the committed .t12/project.json from, "
                   "instead of --repo-config")
    p.add_argument("--out-dir", required=True, help="queue directory bin/t12-queue reads")
    p.add_argument("--home", help="state home; a task with a run there is left alone")
    p.add_argument(f"--{scope[0]}", help=f"{scope[0]}, overriding the profile")
    p.add_argument(f"--{scope[1]}", help=f"only issues with this {scope[1]}, overriding the profile")
    if add_arguments:
        add_arguments(p)
    args = parser.parse_args(argv)

    token = os.environ.get(token_env)
    if not token:
        print(f"error: {token_env} is not set", file=sys.stderr)
        return 2
    try:
        profile = _intake_profile(args, name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    repo = getattr(args, scope[0]) or profile.get(scope[0])
    label = getattr(args, scope[1]) or profile.get(scope[1])
    if not repo or not label:
        # Anyone can file an issue; the label is what a maintainer
        # applies to say "the factory may work on this".
        print(f"error: give --{scope[0]} and --{scope[1]}, or an `intake` key in --repo-config; "
              f"without a {scope[1]} every open issue would become a task", file=sys.stderr)
        return 2
    try:
        issues = list_issues(args, repo, label, token)
    except IntakeError as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return 1
    sync(issues, name, repo, id_prefix, os.path.abspath(args.out_dir), args.home, depends_text)
    return 0


PROFILE_PATH = ".t12/project.json"


def _intake_profile(args, name):
    if args.repo_config and (args.trunk or args.trunk_repo):
        raise ValueError("give --repo-config or --trunk-repo/--trunk, not both")
    if bool(args.trunk) != bool(args.trunk_repo):
        raise ValueError("--trunk-repo and --trunk go together")
    if args.trunk:
        source = f"{PROFILE_PATH} on branch {args.trunk!r}"
        text = _committed_profile(args.trunk_repo, args.trunk)
    elif args.repo_config:
        source = args.repo_config
        try:
            with open(args.repo_config, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ValueError(f"cannot read {source}: {exc}") from exc
    else:
        return {}
    try:
        intake = json.loads(text).get("intake") or {}
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"cannot read {source}: {exc}") from exc
    if not isinstance(intake, dict) or intake.get("adapter") not in (None, name):
        raise ValueError(f"{source} does not name {name!r} as its intake adapter")
    return intake


def _committed_profile(repo, branch):
    """The committed .t12/project.json at the tip of BRANCH; the
    working tree is never read."""
    def git(*cmd):
        return subprocess.run(["git", "-C", repo, *cmd], capture_output=True, text=True)
    head = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/heads/{branch}")
    if head.returncode != 0:
        raise ValueError(f"branch {branch!r} does not exist in {repo}")
    shown = git("show", f"{head.stdout.strip()}:{PROFILE_PATH}")
    if shown.returncode != 0:
        raise ValueError(f"branch {branch!r} in {repo} has no {PROFILE_PATH}")
    return shown.stdout


NATIVE_DEPENDENCIES = {"github": True, "gitlab": False, "asana": False}


def spec_check_main(argv=None):
    """`spec-check`: the body-only rules, for an issue before it is
    filed. Prints JSON {ok, refusals, warnings, new_files}; exit 0 when
    it would pass intake, 1 when refused, 2 on bad arguments. Relations
    (sub-issues, blocked by) are checked at intake, once they exist."""
    parser = argparse.ArgumentParser(prog="spec-check", description=spec_check_main.__doc__)
    parser.add_argument("body", nargs="?", default="-", help="file holding the issue body; - for stdin")
    parser.add_argument("--forge", choices=sorted(NATIVE_DEPENDENCIES), default="github")
    parser.add_argument("--repo-root", help="checkout to report Touches paths that do not exist yet")
    args = parser.parse_args(argv)
    if args.body == "-":
        body = sys.stdin.read()
    else:
        with open(args.body, encoding="utf-8") as fh:
            body = fh.read()
    fields, refusals, warnings = spec_check(body, "x", not NATIVE_DEPENDENCIES[args.forge])
    new_files = []
    if fields and args.repo_root:
        new_files = [t for t in fields["touches"]
                     if not os.path.exists(os.path.join(args.repo_root, t))]
    print(json.dumps({"ok": not refusals, "refusals": refusals, "warnings": warnings,
                      "new_files": new_files}, indent=2))
    return 1 if refusals else 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["spec-check"]:
        raise SystemExit("usage: common.py spec-check [BODY] [--forge F] [--repo-root DIR]")
    raise SystemExit(spec_check_main(sys.argv[2:]))
