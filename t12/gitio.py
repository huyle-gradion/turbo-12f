"""Git invariants. The factory checks git state; it never trusts a
worker's claim about which commit was tested or reviewed.
"""

import os
import shutil

from . import runner
from .errors import GitError

_TIMEOUT = 60


def git(repo, *args, timeout=_TIMEOUT):
    obs = runner.run(["git", *[str(a) for a in args]], cwd=repo, timeout_s=timeout)
    return obs


def git_ok(repo, *args, timeout=_TIMEOUT):
    obs = git(repo, *args, timeout=timeout)
    if obs["timed_out"] or obs["exit_code"] != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit={obs['exit_code']}, "
            f"timed_out={obs['timed_out']}): {(obs['stderr'] or obs['stdout']).strip()[:500]}"
        )
    return obs["stdout"].strip()


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
    out = git_ok(repo, "status", "--porcelain")
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            paths.append(line[3:].strip().strip('"'))
    return paths


def blob_hash(repo, rel_path):
    return git_ok(repo, "hash-object", "--", rel_path)


def is_ancestor(repo, ancestor, descendant):
    obs = git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return obs["exit_code"] == 0 and not obs["timed_out"]


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


def delete_branch(repo, branch):
    git_ok(repo, "branch", "-D", branch)


def commit_file(repo, rel_path, message):
    git_ok(repo, "add", "--", rel_path)
    git_ok(repo, "commit", "-m", message)
    return head(repo)
