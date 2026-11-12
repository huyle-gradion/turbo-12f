"""The builder's cage (issue #4).

`run` starts a builder command under Landlock. The command can write
the run's worktree and a private temp dir. It cannot write the shared
product `.git` or the state home, where a hook, a filter, a replace
ref, or a `.pth` file would run code in the factory's later commands.

A caged builder cannot commit, because a linked worktree's index and
refs live in the shared `.git`. `commit` is the way out: the
orchestrator runs it outside the cage. The worktree's `.git` pointer
file is writable from inside the cage, so `commit` never follows it.
It finds the git dir `prepare` created from the shared `.git` side,
refuses a pointer that names anything else, and passes that git dir
to git explicitly.
"""

import argparse
import os
import signal
import subprocess
import sys
import tempfile

from . import gitio, model, sandbox, store
from .errors import StateError, T12Error

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _overlap(a, b):
    return os.path.commonpath([a, b]) in (a, b)


def _load(home, task_id):
    config_path = os.path.join(home, "config.json")
    if not os.path.exists(config_path):
        raise StateError(f"no turbo-12f state home at {home}; run `init` first")
    repo = store.load_json(config_path)["repo"]
    common_dir = gitio.git_ok(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return model.load_run(home, task_id), common_dir


def prepared_git_dir(common_dir, worktree):
    """The per-worktree git dir `prepare` created for `worktree`, read
    from the shared `.git`, which a caged builder cannot write."""
    admin = os.path.join(common_dir, "worktrees")
    worktree = os.path.realpath(worktree)
    for name in sorted(os.listdir(admin) if os.path.isdir(admin) else ()):
        try:
            with open(os.path.join(admin, name, "gitdir"), encoding="utf-8") as fh:
                dot_git = fh.read().strip()
        except OSError:
            continue
        if (os.path.basename(dot_git) == ".git"
                and os.path.realpath(os.path.dirname(dot_git)) == worktree):
            return os.path.join(admin, name)
    raise StateError(f"the product repo has no worktree registered at {worktree}")


def check_pointer(worktree, git_dir):
    """Refuse a worktree whose `.git` is not the pointer file `prepare`
    wrote. Every later git command in the worktree follows it."""
    expected = f"gitdir: {git_dir}\n".encode()
    path = os.path.join(worktree, ".git")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            actual = fh.read(len(expected) + 1)
    except OSError as exc:
        raise StateError(f"refusing: cannot read the worktree's .git pointer: {exc}") from exc
    if actual != expected:
        raise StateError(
            f"refusing: the worktree's .git pointer changed since prepare "
            f"(expected {expected.decode().strip()!r}); restore it or supersede the run")


def nested_repo(worktree):
    """The first path below the worktree root that holds a `.git`, or
    None. Committed as a gitlink, it makes every later `git status`
    run git inside it, under a config the builder wrote."""
    for root, dirs, files in os.walk(worktree):
        if root != worktree and ".git" in dirs + files:
            return root
        dirs[:] = [d for d in dirs if d != ".git"]
    return None


def commit(home, task_id, message):
    run, common_dir = _load(home, task_id)
    worktree = run["worktree"]
    git_dir = prepared_git_dir(common_dir, worktree)
    check_pointer(worktree, git_dir)

    def git(*args):
        # hooksPath may be relative to the worktree, which the builder writes.
        return gitio.git_ok(
            worktree, f"--git-dir={git_dir}", f"--work-tree={worktree}",
            "-c", "core.hooksPath=/dev/null", *args)

    if git("symbolic-ref", "HEAD") != f"refs/heads/{run['branch']}":
        raise StateError(f"refusing: the worktree is not on {run['branch']!r}")
    nested = nested_repo(worktree)
    if nested:
        raise StateError(f"refusing: nested git repository in the worktree: {nested}")
    git("add", "-A")
    git("commit", "-q", "-m", message)
    print(f"committed: task={task_id} branch={run['branch']} commit={git('rev-parse', 'HEAD')}")
    return 0


def run(home, task_id, argv, read=(), write=(), network=False):
    """Run argv in the cage and return its exit code. Raises before
    the command starts when no cage can be applied."""
    if not sandbox.landlock_available():
        raise StateError(
            "refusing to start: no cage can be applied on this host "
            "(needs Linux Landlock, ABI 4 or later)")
    run_record, common_dir = _load(home, task_id)
    worktree = os.path.realpath(run_record["worktree"])
    common_dir = os.path.realpath(common_dir)
    home = os.path.realpath(home)
    if _overlap(worktree, common_dir):
        raise StateError(f"refusing: worktree {worktree} overlaps the shared git dir")
    with tempfile.TemporaryDirectory(prefix="t12-cage-") as tmp:
        extra = [os.path.realpath(path) for path in (tmp, *write)]
        for path in extra:
            if not os.path.exists(path):
                raise StateError(f"refusing: writable path does not exist: {path}")
            for protected in (common_dir, home):
                if _overlap(path, protected):
                    raise StateError(
                        f"refusing: writable path {path} overlaps {protected}, "
                        "which the cage protects")
        try:
            proc = subprocess.Popen(
                argv, cwd=worktree,
                # git dies on a global config it may not read, and the
                # operator's home is not readable from inside the cage.
                env=dict(os.environ, TMPDIR=tmp, GIT_CONFIG_GLOBAL="/dev/null"),
                # A new session has no controlling terminal to push input into.
                start_new_session=True,
                preexec_fn=sandbox.builder_cage_preexec(
                    [worktree, *extra], [common_dir, *read], network))
        except (OSError, subprocess.SubprocessError) as exc:
            raise StateError(f"refusing to start: cannot cage {argv[0]}: {exc}") from exc
        try:
            return proc.wait()
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _parser(prog, description):
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--home", required=True)
    parser.add_argument("--task-id", required=True)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = None
    if argv[:1] == ["commit"]:
        parser = _parser("t12-cage commit", "Commit a caged builder's edits on the run's branch.")
        parser.add_argument("--message", required=True)
        args = parser.parse_args(argv[1:])
    else:
        parser = _parser("t12-cage", "Run a builder command in the run's cage.")
        parser.usage = ("t12-cage --home H --task-id ID [--read PATH] [--write PATH] "
                        "[--network] -- COMMAND [ARG...]")
        parser.add_argument("--read", action="append", default=[],
                            help="extra path the command may read (repeatable)")
        parser.add_argument("--write", action="append", default=[],
                            help="extra path the command may write (repeatable)")
        parser.add_argument("--network", action="store_true", help="allow TCP")
        split = argv.index("--") if "--" in argv else len(argv)
        args = parser.parse_args(argv[:split])
        command = argv[split + 1:]
        if not command:
            parser.error("no command given after --")
    try:
        home = os.path.realpath(args.home)
        if os.path.commonpath([os.path.realpath(PROJECT_ROOT), home]) == home:
            raise StateError(
                f"refusing: this t12-cage ({PROJECT_ROOT}) runs from inside the state home; "
                "a builder never supplies its own cage, run the installed factory instead")
        if command is None:
            with model.run_lock(home, args.task_id):
                return commit(home, args.task_id, args.message)
        return run(home, args.task_id, command, args.read, args.write, args.network)
    except T12Error as exc:
        print(f"t12-cage: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
