"""What a worker is told: its role's prompt and skills. A project's
own files come from `.t12/` at the run's trunk commit, never the
worktree, so a worker cannot rewrite its own instructions. Anything
the project does not supply comes from the factory checkout.
"""

import os

from . import gitio, store
from .errors import StateError
from .profile import BUILTIN_ROLES


def _entry(source, path, text):
    return {"source": source, "path": path, "text": text,
            "sha256": store.sha256_bytes(text.encode("utf-8"))}


def _project(repo, commit, path):
    text = gitio.file_at(repo, commit, path)
    return None if text is None else _entry("project", path, text)


def _factory(factory_root, path):
    full = os.path.join(factory_root, path)
    if not os.path.isfile(full):
        return None
    with open(full, encoding="utf-8") as fh:
        return _entry("factory", path, fh.read())


def _factory_prompt(role, kind):
    # A project role without its own prompt starts from the prompt of
    # the first built-in role of its kind: a ux-copy reviewer gets
    # prompts/reviewer.md.
    if role not in BUILTIN_ROLES:
        role = next(name for name, k in BUILTIN_ROLES.items() if k == kind)
    return f"prompts/{role}.md"


def resolve(repo, commit, roles, role, factory_root):
    """The role's prompt, then each of its skills, as
    {source, path, sha256, text}, with project files read at `commit`."""
    spec = roles[role]
    if spec["prompt"]:
        path = f".t12/prompts/{spec['prompt']}"
        prompt = _project(repo, commit, path)
    else:
        path = _factory_prompt(role, spec["kind"])
        prompt = _factory(factory_root, path)
    if prompt is None:
        raise StateError(f"the {role} role's prompt {path} is missing")
    used = [prompt]
    for name in spec["skills"]:
        skill = (_project(repo, commit, f".t12/skills/{name}/SKILL.md")
                 or _factory(factory_root, f"skills/{name}/SKILL.md"))
        if skill is None:
            raise StateError(f"the {role} role's skill {name!r} is in neither "
                             f".t12/skills/ on the trunk commit nor the factory's skills/")
        used.append(skill)
    return used
