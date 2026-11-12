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
- protected_paths, security_review_paths: profile only.
"""

import json
import os

from .errors import StateError

PATH = ".t12/project.json"
LEGACY_PATH = ".t12/policy.json"
VERSION = 1


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
OTHER_KEYS = ("version", "name")


def _paths(key, value):
    if not isinstance(value, list) or not all(_relative(p) for p in value):
        raise StateError(f"{PATH}: {key} must be a list of relative paths: {value!r}")
    return sorted({os.path.normpath(p) for p in value})


def _relative(path):
    if not isinstance(path, str) or not path or os.path.isabs(path):
        return False
    return os.path.normpath(path).split(os.sep)[0] not in ("..", ".", ".git")


def parse(text):
    """The profile as stored on the base commit; None means no profile."""
    if text is None:
        return {key: [] for key in SCOPE_KEYS}
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
    data.setdefault("verify_deps", [])
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
    for key in SCOPE_KEYS:
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
