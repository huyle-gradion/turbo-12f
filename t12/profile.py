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
- verify_deps: a task may only narrow the profile's list, and no
  entry may overlap a protected path, since gates load deps from the
  builder's worktree.
- budgets: the task wins.
- protected_paths, security_review_paths, roles, intake, delivery: profile only.

Roles are names a project picks, each mapped to a kind the core
enforces (see tools.TOOLS). The built-in roles are always present; a
project may add roles and give any role a prompt or skills, but may
not change a built-in's kind.
"""

import json
import os
import re

from .errors import StateError
from .tools import KINDS

PATH = ".t12/project.json"
LEGACY_PATH = ".t12/policy.json"
VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
BUILTIN_ROLES = {
    "test-writer": "test-writer",
    "builder": "builder",
    "qa": "qa",
    "reviewer": "review",
    "security": "review",
}


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


TASK_KEYS = {
    "gates": (list, _append),
    "required_reviews": (list, _union),
    "require_probes": (bool, _either),
    "sandbox_adapter": (str, _fixed("sandbox_adapter")),
    "sandbox_image": (str, _fixed("sandbox_image")),
    "verify_deps": (list, _subset),
    "time_limit_s": (int, _task_wins),
    "op_limit": (int, _task_wins),
    "repair_cap": (int, _task_wins),
}
SCOPE_KEYS = ("protected_paths", "security_review_paths")
PROFILE_ONLY = SCOPE_KEYS + ("roles", "intake", "delivery")
OTHER_KEYS = ("version", "name", "roles", "intake", "delivery")
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
    """Where tasks come from. The core never reads it: the intake
    adapter does. Validated here so a typo fails at `prepare`, not at
    the next intake run."""
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


def _delivery(value):
    """Which forge the runner opens change requests on."""
    where = f"{PATH}: delivery"
    if not isinstance(value, dict) or set(value) != {"adapter"}:
        raise StateError(f"{where} must be an object with only an adapter")
    if value["adapter"] not in DELIVERY_ADAPTERS:
        raise StateError(f"{where}: adapter must be one of {list(DELIVERY_ADAPTERS)}")
    return {"adapter": value["adapter"]}


def review_roles(roles):
    return sorted(name for name, spec in roles.items() if spec["kind"] == "review")


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
    for key, (kind, _) in TASK_KEYS.items():
        if key in data:
            _check_type(PATH, key, data[key], kind)
    for key in SCOPE_KEYS:
        data[key] = _paths(key, data.get(key, []))
    # A profile that lists no deps allows none: otherwise a task's list
    # would win outright, and deps load from the builder's worktree.
    if text is not None:
        data.setdefault("verify_deps", [])
    data["roles"] = _roles(data.get("roles", {}))
    if "intake" in data:
        data["intake"] = _intake(data["intake"])
    if "delivery" in data:
        data["delivery"] = _delivery(data["delivery"])
    reviewers = review_roles(data["roles"])
    unknown = [r for r in data.get("required_reviews", []) if r not in reviewers]
    if unknown:
        raise StateError(f"{PATH}: required_reviews {unknown} are not review roles; "
                         f"review roles are {reviewers}")
    return data


ELEMENTS = {"gates": dict, "required_reviews": str, "verify_deps": str}


def _check_type(where, key, value, kind):
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise StateError(f"{where}: {key} must be a {kind.__name__}")
    element = ELEMENTS.get(key)
    if element and not all(isinstance(v, element) for v in value):
        raise StateError(f"{where}: every {key} entry must be a {element.__name__}")
    if key == "gates" and not all(isinstance(g.get("name"), str) and g["name"] for g in value):
        raise StateError(f"{where}: every gate needs a non-empty string name")


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
    # A dep beside a protected file can shadow it too, as a __pycache__
    # next to a protected test does, so each protected path's folder
    # counts. Root-level files are the exception: their folder is the
    # whole repo.
    roots = set(profile["protected_paths"])
    roots |= {os.path.dirname(p) for p in profile["protected_paths"] if os.path.dirname(p)}
    for dep in merged.get("verify_deps", []):
        dep_path = os.path.normpath(dep)
        for root in sorted(roots):
            if dep_path == root or dep_path.startswith(root + os.sep) or root.startswith(dep_path + os.sep):
                raise StateError(f"verify_deps entry {dep!r} overlaps protected path {root!r}")
    return merged


def security_roots(profile):
    """Paths whose change needs a security review. `.t12/` is always
    one, so a candidate can neither loosen the profile nor add the
    first one without a security review."""
    return profile["security_review_paths"] + [os.path.dirname(PATH)]
