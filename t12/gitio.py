"""Git invariants. The factory checks git state; it never trusts a
worker's claim about which commit was tested or reviewed.
"""

import contextlib
import os
import shutil

from . import runner
from .errors import GitError

_TIMEOUT = 60


def git(repo, *args, timeout=_TIMEOUT, env=None):
    obs = runner.run(["git", *[str(a) for a in args]], cwd=repo, timeout_s=timeout, env=env)
    return obs


def git_ok(repo, *args, timeout=_TIMEOUT, env=None, strip=True):
    obs = git(repo, *args, timeout=timeout, env=env)
    if obs["timed_out"] or obs["exit_code"] != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit={obs['exit_code']}, "
            f"timed_out={obs['timed_out']}): {(obs['stderr'] or obs['stdout']).strip()[:500]}"
        )
    return obs["stdout"].strip() if strip else obs["stdout"]


def work_diff(repo, base, exclude=None):
    """Everything the worktree changed since `base`, committed or not,
    new files included, optionally leaving one path out."""
    git_ok(repo, "add", "-A", "-N")
    spec = ["--", "."] + ([f":(exclude){exclude}"] if exclude else [])
    return git_ok(repo, "diff", base, *spec, strip=False)


def is_git_repo(path):
    if not os.path.isdir(path):
        return False
    try:
        git_ok(path, "rev-parse", "--git-dir")
        return True
    except GitError:
        return False


def head(repo):
    return git_ok(repo, "rev-parse", "HEAD")


def rev_parse(repo, ref):
    return git_ok(repo, "rev-parse", "--verify", ref)


def branch_exists(repo, branch):
    obs = git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    return obs["exit_code"] == 0 and not obs["timed_out"]


def is_clean(repo):
    return git_ok(repo, "status", "--porcelain") == ""


def dirty_paths(repo):
    """Every changed, staged, or untracked path, exactly as git names
    it. A rename lists both its new and its old path."""
    obs = git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    if obs["timed_out"] or obs["exit_code"] != 0:
        raise GitError(f"git status failed in {repo}: {obs['stderr'].strip()[:500]}")
    entries = obs["stdout"].split("\0")
    paths = []
    while entries:
        entry = entries.pop(0)
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC" and entries:
                paths.append(entries.pop(0))
    return paths


def blob_hash(repo, rel_path):
    return git_ok(repo, "hash-object", "--", rel_path)


def is_ancestor(repo, ancestor, descendant):
    obs = git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return obs["exit_code"] == 0 and not obs["timed_out"]


def git_ok_bool(repo, *args):
    obs = git(repo, *args)
    return obs["exit_code"] == 0 and not obs["timed_out"]


def merge_base(repo, a, b):
    return git_ok(repo, "merge-base", a, b)


def changed_paths(repo, a, b):
    """Every path that differs between two commits. A rename counts as
    both its old and its new path."""
    obs = git(repo, "diff", "--name-only", "--no-renames", "-z", a, b)
    if obs["timed_out"] or obs["exit_code"] != 0:
        raise GitError(f"git diff {a} {b} failed: {obs['stderr'].strip()[:500]}")
    return [p for p in obs["stdout"].split("\0") if p]


def current_branch(repo):
    return git_ok(repo, "branch", "--show-current")


def add_worktree(repo, worktree_path, branch, base):
    git_ok(repo, "worktree", "add", "-b", branch, str(worktree_path), base)


def remove_worktree(repo, worktree_path):
    git_ok(repo, "worktree", "remove", "--force", str(worktree_path))


def remove_worktree_force(repo, worktree_path):
    """Remove a worktree even if its metadata is stale: try the normal
    removal, then prune and remove the directory."""
    obs = git(repo, "worktree", "remove", "--force", str(worktree_path))
    if obs["exit_code"] == 0 and not obs["timed_out"]:
        return
    git(repo, "worktree", "prune")
    if os.path.isdir(worktree_path):
        shutil.rmtree(worktree_path)


@contextlib.contextmanager
def detached_checkout(repo, path, commit):
    """A fresh checkout of exactly `commit`, removed on exit. Nothing
    untracked, ignored, or written later in the builder's worktree is
    visible inside it."""
    remove_worktree_force(repo, path)
    git_ok(repo, "worktree", "add", "--detach", str(path), commit)
    try:
        yield path
    finally:
        remove_worktree_force(repo, path)


def commit_with_path(repo, rel_path, message):
    """A commit of HEAD plus `rel_path` as it is in the worktree, built
    in a private index. No branch and no real index move."""
    index = os.path.join(git_ok(repo, "rev-parse", "--absolute-git-dir"), "t12-index")
    env = {"GIT_INDEX_FILE": index}
    try:
        git_ok(repo, "read-tree", "HEAD", env=env)
        git_ok(repo, "add", "--", rel_path, env=env)
        tree = git_ok(repo, "write-tree", env=env)
        return git_ok(repo, "commit-tree", tree, "-p", "HEAD", "-m", message)
    finally:
        if os.path.exists(index):
            os.remove(index)


def file_at(repo, commit, rel_path):
    """The file's text at `commit`, or None when the commit lacks it.
    A folder at the path is an error, not an absence: reading the
    profile as missing would drop every rule in it."""
    obs = git(repo, "cat-file", "-t", f"{commit}:{rel_path}")
    if obs["exit_code"] != 0:
        return None
    if obs["stdout"].strip() != "blob":
        raise GitError(f"{rel_path} at {commit[:12]} is a {obs['stdout'].strip()}, not a file")
    return git_ok(repo, "show", f"{commit}:{rel_path}", strip=False)


def blob_at(repo, commit, rel_path):
    return git_ok(repo, "rev-parse", f"{commit}:{rel_path}")


def move_branch_to(repo, commit):
    """Point the checked-out branch and the index at `commit`, leaving
    worktree files as they are."""
    git_ok(repo, "reset", "-q", commit)


def delete_branch(repo, branch):
    git_ok(repo, "branch", "-D", branch)
