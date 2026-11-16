"""The project profile: `.t12/project.json` in the product repo.

It holds what every task in a project shares, so a task file only
says what is unique to the task. `prepare` reads it from the trunk
the state home names, never the task's base or the worktree, so a run
cannot choose or change the rules it is checked by. Pure: text in,
data out.

A task may tighten what the profile sets but never loosen it. Task
files can come from an issue tracker, written by anyone who can file
an issue, so the profile is the floor:

- gates: the profile's gates run first; a task may append more.
- required_reviews: the union of both.
- require_probes: on if either turns it on.
- sandbox_adapter, sandbox_image: a task may set them only where the
  profile does not.
- test_command, test_globs, lint_command, assertion_pattern,
  failure_count_pattern, red_stub_max_lines, refactor_skip_max_lines: the tdd loop's test and
  lint settings; a task may set one only where the profile does not.
  test_command is required, from one or the other. test_globs says
  which files are tests (red writes them, green and refactor cannot
  see them); it defaults to tests/**. lint_command defaults to none,
  assertion_pattern to AssertionError, red_stub_max_lines to 10. Green
  patches of at most refactor_skip_max_lines production lines (added plus
  deleted, summed since the last refactor; default 5, 0 = never skip) skip
  the refactor step unless structural directives are pending.
- verify_deps: a task may only narrow the profile's list, and no
  entry may overlap a protected path, since gates load deps from the
  worker's worktree.
- budgets (time_limit_s, op_limit, repair_cap, cycle_limit,
  attempt_limit): the task wins.
- max_lanes: how many tdd lanes may run at once (default 3, the task
  wins); 1 takes the serial path whatever units the analyzer declares.
- protected_paths, security_review_paths, roles, intake, skills,
  delivery: profile only. `skills` pins third-party skills per role
  (repo, commit, hash); the core checks their shape and picks a role's
  applicable pins from the repo at the base commit, and never fetches
  anything. `delivery` names the forge the runner opens the change
  request on once a run is local-verified.

Roles are names a project picks, each mapped to a kind the core
enforces (see tools.TOOLS). The built-in roles are always present; a
project may add roles and give any role a prompt or skills, but may
not change a built-in's kind.
"""

import json
import os
import re
import shlex

from . import gitio, globs
from .errors import RunnerError, StateError
from .tools import KINDS

PATH = ".t12/project.json"
LEGACY_PATH = ".t12/policy.json"
VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
BUILTIN_ROLES = {
    "analyzer": "analyzer",
    "red": "red",
    "green": "green",
    "refactor": "refactor",
    "reviewer": "review",
}
DEFAULT_TEST_GLOBS = ["tests/**"]
DEFAULT_ASSERTION_PATTERN = "AssertionError"
DEFAULT_RED_STUB_MAX_LINES = 10
DEFAULT_REFACTOR_SKIP_MAX_LINES = 5
DEFAULT_IDLE_AFTER_S = 600


def _append(profile_value, task_value):
    names = {g.get("name") for g in profile_value}
    clash = sorted(n for n in (g.get("name") for g in task_value) if n in names)
    if clash:
        raise StateError(f"task gates {clash} clash with the project profile's gates")
    return list(profile_value) + list(task_value)


def _union(profile_value, task_value):
    return list(profile_value) + [r for r in task_value if r not in profile_value]


def _either(profile_value, task_value):
    return bool(profile_value) or bool(task_value)


def _fixed(key):
    def merge(profile_value, task_value):
        if task_value != profile_value:
            raise StateError(
                f"task sets {key}={task_value!r} but the project profile fixes it "
                f"at {profile_value!r}")
        return profile_value
    return merge


def _subset(profile_value, task_value):
    wider = sorted(set(map(os.path.normpath, task_value)) -
                   set(map(os.path.normpath, profile_value)))
    if wider:
        raise StateError(f"task verify_deps {wider} are not in the project profile's list")
    return task_value


def _task_wins(profile_value, task_value):
    return task_value


NONE = type(None)
TASK_KEYS = {
    "gates": (list, _append),
    "required_reviews": (list, _union),
    "require_probes": (bool, _either),
    "sandbox_adapter": (str, _fixed("sandbox_adapter")),
    "sandbox_image": (str, _fixed("sandbox_image")),
    "test_command": (list, _fixed("test_command")),
    "test_globs": (list, _fixed("test_globs")),
    "lint_command": ((list, NONE), _fixed("lint_command")),
    "assertion_pattern": (str, _fixed("assertion_pattern")),
    "failure_count_pattern": ((str, NONE), _fixed("failure_count_pattern")),
    "red_stub_max_lines": (int, _fixed("red_stub_max_lines")),
    "refactor_skip_max_lines": (int, _fixed("refactor_skip_max_lines")),
    "verify_deps": (list, _subset),
    "time_limit_s": (int, _task_wins),
    "op_limit": (int, _task_wins),
    "repair_cap": (int, _task_wins),
    "cycle_limit": (int, _task_wins),
    "attempt_limit": (int, _task_wins),
    "max_lanes": (int, _task_wins),
}
SCOPE_KEYS = ("protected_paths", "security_review_paths")
PROFILE_ONLY = SCOPE_KEYS + ("roles", "intake", "skills", "delivery", "jobs", "review_criteria")
OTHER_KEYS = ("version", "name", "roles", "intake", "skills", "delivery", "jobs",
              "review_criteria", "idle_after_s")
DELIVERY_ADAPTERS = ("github", "gitlab", "bitbucket")


def _paths(key, value):
    if not isinstance(value, list) or not all(_relative(p) for p in value):
        raise StateError(f"{PATH}: {key} must be a list of relative paths: {value!r}")
    return sorted({os.path.normpath(p) for p in value})


def _relative(path):
    if not isinstance(path, str) or not path or os.path.isabs(path):
        return False
    return os.path.normpath(path).split(os.sep)[0] not in ("..", ".", ".git")


def _roles(value):
    if not isinstance(value, dict):
        raise StateError(f"{PATH}: roles must be an object of role name to role")
    roles = {name: {"kind": kind, "prompt": None, "skills": []}
             for name, kind in BUILTIN_ROLES.items()}
    for name, spec in value.items():
        where = f"{PATH}: role {name!r}"
        if not NAME_RE.match(name):
            raise StateError(f"{where}: a role name must match {NAME_RE.pattern}")
        if not isinstance(spec, dict) or set(spec) - {"kind", "prompt", "skills"}:
            raise StateError(f"{where} must be an object of kind, prompt and skills")
        kind = spec.get("kind", BUILTIN_ROLES.get(name))
        if kind not in KINDS:
            raise StateError(f"{where}: kind must be one of {list(KINDS)}, got {kind!r}")
        if name in BUILTIN_ROLES and kind != BUILTIN_ROLES[name]:
            raise StateError(f"{where} is built in as kind {BUILTIN_ROLES[name]!r}; "
                             "a project cannot change its kind")
        prompt = spec.get("prompt")
        if prompt is not None and not _relative(prompt):
            raise StateError(f"{where}: prompt must be a relative path under .t12/prompts/")
        skills = spec.get("skills", [])
        if not isinstance(skills, list) or not all(
                isinstance(n, str) and NAME_RE.match(n) for n in skills):
            raise StateError(f"{where}: skills must be a list of names matching {NAME_RE.pattern}")
        if prompt is not None:
            prompt = os.path.normpath(prompt)
        roles[name] = {"kind": kind, "prompt": prompt, "skills": list(skills)}
    return roles


def _intake(value):
    where = f"{PATH}: intake"
    if isinstance(value, dict) and value.get("adapter") == "asana-tasks":
        if set(value) - {"adapter", "project", "tag"}:
            raise StateError(f"{where} must be an object of adapter, project and tag")
        for key in ("project", "tag"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise StateError(f"{where}: {key} must be a non-empty string")
        return dict(value)
    if not isinstance(value, dict) or set(value) - {"adapter", "repo", "label"}:
        raise StateError(f"{where} must be an object of adapter, repo and label")
    adapter, repo, label = value.get("adapter"), value.get("repo"), value.get("label")
    if not isinstance(adapter, str) or not NAME_RE.match(adapter):
        raise StateError(f"{where}: adapter must be a name matching {NAME_RE.pattern}")
    if not isinstance(repo, str) or not re.fullmatch(r"[^\s/]+(/[^\s/]+)+", repo):
        raise StateError(f"{where}: repo must look like owner/name")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise StateError(f"{where}: label must be a non-empty string")
    return {k: v for k, v in (("adapter", adapter), ("repo", repo), ("label", label))
            if v is not None}


SKILL_KEYS = {"repo", "rev", "path", "include", "wrap", "roles", "when", "sha256"}
SKILL_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SKILL_REV_RE = re.compile(r"^[0-9a-f]{40}$")
SKILL_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _skill_path(path):
    return (isinstance(path, str) and bool(path) and not os.path.isabs(path)
            and ".." not in path.split("/") and "\\" not in path)


def _skill_when(where, when):
    if when is None:
        return None
    if (not isinstance(when, dict) or len(when) != 1
            or next(iter(when)) not in ("dependency", "files")):
        raise StateError(f"{where}: when must be null, {{\"dependency\": name}} "
                         "or {\"files\": [globs]}")
    key, value = next(iter(when.items()))
    if key == "dependency":
        if not isinstance(value, str) or not value:
            raise StateError(f"{where}: when.dependency must be a package name")
    elif (not isinstance(value, list) or not value
          or not all(isinstance(g, str) and g for g in value)):
        raise StateError(f"{where}: when.files must be a non-empty list of globs")
    return {key: value}


def _skills(value, roles):
    if not isinstance(value, dict):
        raise StateError(f"{PATH}: skills must be an object of skill name to pin")
    skills = {}
    for name, pin in value.items():
        where = f"{PATH}: skill {name!r}"
        if not NAME_RE.match(name):
            raise StateError(f"{where}: a skill name must match {NAME_RE.pattern}")
        if not isinstance(pin, dict) or set(pin) - SKILL_KEYS:
            raise StateError(f"{where} must be an object of {sorted(SKILL_KEYS)}")
        repo, rev, path = pin.get("repo"), pin.get("rev"), pin.get("path")
        if not isinstance(repo, str) or not SKILL_REPO_RE.match(repo):
            raise StateError(f"{where}: repo must look like owner/name")
        if not isinstance(rev, str) or not SKILL_REV_RE.match(rev):
            raise StateError(f"{where}: rev must be a 40-hex commit sha")
        if not _skill_path(path):
            raise StateError(f"{where}: path must be a relative path inside the repo, "
                             f"without '..': {path!r}")
        wrap = pin.get("wrap", False)
        if not isinstance(wrap, bool):
            raise StateError(f"{where}: wrap must be true or false")
        include = pin.get("include", [])
        if (not isinstance(include, list) or not all(_skill_path(g) for g in include)
                or (not wrap and not include)):
            raise StateError(f"{where}: include must be a list of relative globs, "
                             "non-empty unless the skill is wrapped")
        if wrap and include:
            raise StateError(f"{where}: a wrapped skill is one file; it takes no include")
        for_roles = pin.get("roles")
        if (not isinstance(for_roles, list) or not for_roles
                or not all(isinstance(r, str) and r in roles for r in for_roles)):
            raise StateError(f"{where}: roles must be a non-empty list of this run's "
                             f"roles {sorted(roles)}")
        sha256 = pin.get("sha256")
        if not isinstance(sha256, str) or not SKILL_SHA256_RE.match(sha256):
            raise StateError(f"{where}: sha256 must be the 64-hex hash of the synced "
                             "tree; run adapters/skills.py sync --update-hashes")
        skills[name] = {"repo": repo, "rev": rev, "path": path, "include": list(include),
                        "wrap": wrap, "roles": list(dict.fromkeys(for_roles)),
                        "when": _skill_when(where, pin.get("when")), "sha256": sha256}
    return skills


def applicable_skills(run_profile, role, repo, commit):
    """The pins `role` gets on this run: [{name, rev, sha256}], by name.
    A pin's `when` is read from the repo at `commit` (package.json
    dependencies, tracked files): git only, no network."""
    pins = {name: pin for name, pin in (run_profile.get("skills") or {}).items()
            if role in pin["roles"]}
    if not pins:
        return []
    manifest = None
    paths = None
    chosen = []
    for name in sorted(pins):
        pin = pins[name]
        when = pin["when"]
        if when is not None and "dependency" in when:
            if manifest is None:
                text = gitio.file_at(repo, commit, "package.json")
                try:
                    data = json.loads(text) if text is not None else {}
                except ValueError as exc:
                    raise StateError(f"package.json at {commit[:12]} is not valid JSON: {exc}") from exc
                if not isinstance(data, dict):
                    raise StateError(f"package.json at {commit[:12]} must be a JSON object")
                manifest = {}
                for table in ("dependencies", "devDependencies"):
                    if isinstance(data.get(table), dict):
                        manifest.update(data[table])
            if when["dependency"] not in manifest:
                continue
        elif when is not None:
            if paths is None:
                paths = list(gitio.blob_map(repo, commit, lambda p: True))
            if not any(globs.match(p, when["files"]) for p in paths):
                continue
        chosen.append({"name": name, "rev": pin["rev"], "sha256": pin["sha256"]})
    return chosen


def _delivery(value):
    where = f"{PATH}: delivery"
    if not isinstance(value, dict) or set(value) != {"adapter"}:
        raise StateError(f"{where} must be an object with only an adapter")
    if value["adapter"] not in DELIVERY_ADAPTERS:
        raise StateError(f"{where}: adapter must be one of {list(DELIVERY_ADAPTERS)}")
    return {"adapter": value["adapter"]}


JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
JOB_KEYS = {"name", "command", "adapter", "timeout_s", "config", "label"}


def _check_job(job):
    from . import sandbox
    where = f"{PATH}: job {job.get('name')!r}"
    unknown = sorted(set(job) - JOB_KEYS)
    if unknown:
        raise StateError(f"{where} has unknown keys {unknown}")
    if "label" in job and (not isinstance(job["label"], str) or not job["label"].strip()):
        raise StateError(f"{where}: label must be a non-empty string")
    if "command" in job and "adapter" in job:
        raise StateError(f"{where} has both command and adapter; "
                         "a job is one or the other")
    if "command" not in job and "adapter" not in job:
        raise StateError(f"{where} has neither command nor adapter; "
                         "a job is one or the other")
    if "adapter" in job and "timeout_s" in job:
        raise StateError(f"{where} is an adapter job and "
                         "cannot set timeout_s")
    if "command" in job and "config" in job:
        raise StateError(f"{where} is a command job and "
                         "cannot set config")
    if "command" in job:
        try:
            command = job["command"]
            if not isinstance(command, (str, list)):
                raise StateError(f"{where}: command must be a string or a list")
            if isinstance(command, str):
                command = shlex.split(command)
            if not command:
                raise StateError(f"{where}: command must not be empty")
            if not all(isinstance(item, str) for item in command):
                raise StateError(f"{where}: command items must be strings")
            if not all(item for item in command):
                raise StateError(f"{where}: command items must not be empty")
            sandbox.check_command(command)
            if isinstance(job["command"], str):
                raise StateError(f"{where}: command must be a list, not a string")
        except (RunnerError, ValueError) as exc:
            raise StateError(f"{where}: {exc}") from exc


def _jobs(value):
    if not isinstance(value, list):
        raise StateError(f"{PATH}: jobs must be a list")
    for job in value:
        if not isinstance(job, dict):
            raise StateError(f"{PATH}: job {job!r} must be an object")
        name = job.get("name")
        if not isinstance(name, str) or not JOB_NAME_RE.match(name):
            raise StateError(f"{PATH}: job name {name!r} must match {JOB_NAME_RE.pattern}")
    for job in value:
        _check_job(job)
    names = [job.get("name") for job in value]
    for name in names:
        if names.count(name) > 1:
            raise StateError(f"{PATH}: job name {name!r} is used twice")
    return value


def review_roles(roles):
    return sorted(name for name, spec in roles.items() if spec["kind"] == "review")


def _allow_no_deps_unless_listed(data):
    data.setdefault("verify_deps", [])


def _protected_paths_and_their_folders(protected_paths):
    return set(protected_paths) | {os.path.dirname(p) for p in protected_paths if os.path.dirname(p)}


def _check_idle_after_s(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise StateError(f"{PATH}: idle_after_s must be an integer above 0, got {value!r}")


def parse(text):
    """The profile as stored on the trunk commit; None means no profile."""
    if text is None:
        data = {"version": VERSION}
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StateError(f"{PATH} on the base commit is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"{PATH} on the base commit must be a JSON object")
    unknown = sorted(set(data) - set(TASK_KEYS) - set(SCOPE_KEYS) - set(OTHER_KEYS))
    if unknown:
        raise StateError(f"{PATH}: unknown keys {unknown}")
    if data.get("version") != VERSION:
        raise StateError(f"{PATH}: version must be {VERSION}, got {data.get('version')!r}")
    if "idle_after_s" in data:
        _check_idle_after_s(data["idle_after_s"])
    for key, (kind, _) in TASK_KEYS.items():
        if key in data:
            _check_type(PATH, key, data[key], kind)
    for key in SCOPE_KEYS:
        data[key] = _paths(key, data.get(key, []))
    if text is not None:
        _allow_no_deps_unless_listed(data)
    data["roles"] = _roles(data.get("roles", {}))
    if "intake" in data:
        data["intake"] = _intake(data["intake"])
    if "skills" in data:
        data["skills"] = _skills(data["skills"], data["roles"])
    if "delivery" in data:
        data["delivery"] = _delivery(data["delivery"])
    if "jobs" in data:
        data["jobs"] = _jobs(data["jobs"])
    if "review_criteria" in data and not isinstance(data["review_criteria"], bool):
        raise StateError(f"{PATH}: review_criteria must be a bool")
    reviewers = review_roles(data["roles"])
    unknown = [r for r in data.get("required_reviews", []) if r not in reviewers]
    if unknown:
        raise StateError(f"{PATH}: required_reviews {unknown} are not review roles; "
                         f"review roles are {reviewers}")
    return data


ELEMENTS = {"gates": dict, "required_reviews": str, "verify_deps": str,
            "test_command": str, "test_globs": str, "lint_command": str}


def _type_name(kind):
    if isinstance(kind, tuple):
        return " or ".join("null" if k is NONE else k.__name__ for k in kind)
    return kind.__name__


def _check_pattern(where, key, value, groups=0):
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise StateError(f"{where}: {key} is not a valid regular expression: {exc}") from exc
    if compiled.groups < groups:
        raise StateError(f"{where}: {key} needs a capture group for the count")


def _check_value(where, key, value):
    if key in ("test_command", "lint_command") and value is not None:
        if not value or not all(v.strip() for v in value):
            raise StateError(f"{where}: {key} must be a non-empty argv of non-empty strings")
    elif key == "test_globs":
        bad = [g for g in value if not g.strip() or os.path.isabs(g) or ".." in g.split("/")]
        if not value or bad:
            raise StateError(f"{where}: test_globs must be a non-empty list of relative "
                             f"globs: {value!r}")
    elif key == "assertion_pattern":
        _check_pattern(where, key, value)
    elif key == "failure_count_pattern" and value is not None:
        _check_pattern(where, key, value, groups=1)
    elif key in ("red_stub_max_lines", "refactor_skip_max_lines") and value < 0:
        raise StateError(f"{where}: {key} must be >= 0")
    elif key in ("cycle_limit", "attempt_limit", "max_lanes") and value <= 0:
        raise StateError(f"{where}: {key} must be > 0")


def _check_type(where, key, value, kind):
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise StateError(f"{where}: {key} must be a {_type_name(kind)}")
    element = ELEMENTS.get(key)
    if element and isinstance(value, list) and not all(isinstance(v, element) for v in value):
        raise StateError(f"{where}: every {key} entry must be a {element.__name__}")
    if key == "gates" and not all(isinstance(g.get("name"), str) and g["name"] for g in value):
        raise StateError(f"{where}: every gate needs a non-empty string name")
    _check_value(where, key, value)


def _reject_deps_overlapping_protected(deps, roots):
    for dep in deps:
        dep_path = os.path.normpath(dep)
        for root in sorted(roots):
            if dep_path == root or dep_path.startswith(root + os.sep) or root.startswith(dep_path + os.sep):
                raise StateError(f"verify_deps entry {dep!r} overlaps protected path {root!r}")


def apply(profile, task):
    """The task as the run sees it: profile defaults merged under the
    rules in this module's docstring."""
    for key in PROFILE_ONLY:
        if key in task:
            raise StateError(f"{key} belongs in {PATH}, not in a task file")
    merged = dict(task)
    for key, (kind, merge) in TASK_KEYS.items():
        if key in task:
            _check_type("task", key, task[key], kind)
        if key not in profile:
            continue
        merged[key] = merge(profile[key], task[key]) if key in task else profile[key]
    _reject_deps_overlapping_protected(
        merged.get("verify_deps", []),
        _protected_paths_and_their_folders(profile["protected_paths"]))
    merged.setdefault("test_globs", list(DEFAULT_TEST_GLOBS))
    merged.setdefault("lint_command", None)
    merged.setdefault("assertion_pattern", DEFAULT_ASSERTION_PATTERN)
    merged.setdefault("failure_count_pattern", None)
    merged.setdefault("red_stub_max_lines", DEFAULT_RED_STUB_MAX_LINES)
    merged.setdefault("refactor_skip_max_lines", DEFAULT_REFACTOR_SKIP_MAX_LINES)
    if not merged.get("test_command"):
        raise StateError(f"test_command is required: set it in {PATH} or the task file")
    return merged


def security_roots(profile):
    """Paths whose change the reviewer is told about. `.t12/` is always
    one, so a candidate that loosens the profile or adds the first
    security path is flagged to the review."""
    return profile["security_review_paths"] + [os.path.dirname(PATH)]
