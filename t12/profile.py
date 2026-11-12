"""The project profile: `.t12/project.json` in the product repo.

It holds what every task in a project shares, so a task file only
says what is unique to the task. `prepare` reads it from the base
commit, never the worktree, so a run cannot change the rules it is
checked by. Pure: text in, data out.

A task may tighten what the profile sets but never loosen it. Task
files can come from an issue tracker, written by anyone who can file
an issue, so the profile is the floor:

- gates: the profile's gates run first; a task may append more.
- required_reviews: the union of both.
- require_probes: on if either turns it on.
- sandbox_adapter, sandbox_image: a task may set them only where the
  profile does not.
- budgets and verify_deps: the task wins.
- protected_paths, security_review_paths: profile only.
"""

import json
import os

from .errors import StateError

PATH = ".t12/project.json"
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


def _task_wins(profile_value, task_value):
    return task_value


TASK_KEYS = {
    "gates": (list, _append),
    "required_reviews": (list, _union),
    "require_probes": (bool, _either),
    "sandbox_adapter": (str, _fixed("sandbox_adapter")),
    "sandbox_image": (str, _fixed("sandbox_image")),
    "verify_deps": (list, _task_wins),
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
        if key in data and (not isinstance(data[key], kind) or
                            (kind is int and isinstance(data[key], bool))):
            raise StateError(f"{PATH}: {key} must be a {kind.__name__}")
    for key in SCOPE_KEYS:
        data[key] = _paths(key, data.get(key, []))
    return data


def apply(profile, task):
    """The task as the run sees it: profile defaults merged under the
    rules in this module's docstring."""
    for key in SCOPE_KEYS:
        if key in task:
            raise StateError(f"{key} belongs in {PATH}, not in a task file")
    merged = dict(task)
    for key, (_, merge) in TASK_KEYS.items():
        if key not in profile:
            continue
        merged[key] = merge(profile[key], task[key]) if key in task else profile[key]
    return merged


def security_roots(profile):
    """Paths whose change needs a security review. `.t12/` is always
    one, so a candidate can neither loosen the profile nor add the
    first one without a security review."""
    return profile["security_review_paths"] + [os.path.dirname(PATH)]
