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

`run` starts the command in a new PID namespace. The kernel kills
every process in that namespace when its first process dies, and that
process dies with `t12-cage`. So nothing caged outlives `t12-cage` to
rewrite the pointer after `commit` has checked it, whether `t12-cage`
returns or is killed. Nothing in the namespace can name a process
outside it by pid, `t12-cage` included.
"""

import argparse
import ctypes
import errno
import os
import signal
import subprocess
import sys
import tempfile

from . import gitio, model, sandbox, store
from .errors import StateError, T12Error

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWPID = 0x20000000
_PR_SET_PDEATHSIG = 1
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_XDG_DIRS = {
    "XDG_CONFIG_HOME": "config",
    "XDG_CACHE_HOME": "cache",
    "XDG_DATA_HOME": "data",
    "XDG_STATE_HOME": "state",
}


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


def _enter_namespaces():
    """Move this process into a new user namespace, and make its next
    child the first process of a new PID namespace. An unprivileged
    process may create a PID namespace only together with a user
    namespace. The user namespace maps the operator's uid and gid to
    themselves, so it grants nothing outside itself."""
    uid, gid = os.getuid(), os.getgid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(_CLONE_NEWUSER | _CLONE_NEWPID) != 0:
        raise StateError(
            "refusing to start: cannot create a PID namespace to hold the command's "
            f"processes ({os.strerror(ctypes.get_errno())}); "
            "this host must allow unprivileged user namespaces")
    try:
        for name, text in (("uid_map", f"{uid} {uid} 1"), ("setgroups", "deny"),
                           ("gid_map", f"{gid} {gid} 1")):
            with open(f"/proc/self/{name}", "w", encoding="ascii") as fh:
                fh.write(text)
    except OSError as exc:
        if exc.errno != errno.EACCES:
            raise StateError(f"refusing to start: cannot map the user namespace: {exc}") from exc
        # An outer Landlock cage with a read-only /proc, like the one
        # the factory's own gates run in. File access is unchanged.
        print("t12-cage: cannot write /proc/self/uid_map; "
              "the command sees its uid and every file owner as 65534", file=sys.stderr)


def _exit_code(status):
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def _init(parent_alive, argv, **popen):
    """Be the PID namespace's first process: start the command, reap
    what is reparented here, and exit with the command's exit code.
    Never returns. Any way this process ends kills the namespace."""
    code = 1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
        # End of file here means t12-cage died before the line above.
        if os.read(parent_alive, 1):
            os.close(parent_alive)
            # A new session has no controlling terminal to push input into.
            command = subprocess.Popen(argv, start_new_session=True, **popen)
            while True:
                pid, status = os.waitpid(-1, 0)
                if pid == command.pid:
                    code = _exit_code(status)
                    break
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"t12-cage: refusing to start: cannot cage {argv[0]}: {exc}", file=sys.stderr)
    finally:
        os._exit(code)


def _raise_exit(signum, _frame):
    raise SystemExit(128 + signum)


def _run_contained(argv, **popen):
    """Run argv as a child of a new PID namespace's first process and
    return its exit code. Moves the calling process into a new user
    namespace, so call it once, from a single-threaded process."""
    _enter_namespaces()
    parent_alive, alive = os.pipe()
    try:
        init = os.fork()
    except OSError as exc:
        raise StateError(f"refusing to start: cannot start the PID namespace: {exc}") from exc
    if init == 0:
        os.close(alive)
        _init(parent_alive, argv, **popen)
    os.close(parent_alive)
    os.write(alive, b"1")
    os.close(alive)
    status = None
    # A stop signal before these handlers are set kills this process,
    # and the kernel then kills the namespace.
    previous = {sig: signal.signal(sig, _raise_exit) for sig in _STOP_SIGNALS}
    try:
        status = os.waitpid(init, 0)[1]
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        if status is None:
            os.kill(init, signal.SIGKILL)
            status = os.waitpid(init, 0)[1]
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return _exit_code(status)


def run(home, task_id, argv, read=(), write=(), network=False):
    """Run argv in the cage and return its exit code. Raises before
    the command starts when no cage can be applied. When it returns,
    no process argv started is alive. Call it once per process."""
    # The cage's ruleset names an access right that ABI 5 added.
    if not sandbox.landlock_available() or sandbox.landlock_abi() < 5:
        raise StateError(
            "refusing to start: no cage can be applied on this host "
            "(needs Linux Landlock, ABI 5 or later)")
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
        # git dies on a config or exclude file it may not read, and the
        # operator's home is not readable from inside the cage.
        env = dict(os.environ, TMPDIR=tmp, GIT_CONFIG_GLOBAL="/dev/null")
        for key, name in _XDG_DIRS.items():
            env[key] = os.path.join(tmp, name)
            os.mkdir(env[key])
        return _run_contained(
            argv, cwd=worktree, env=env,
            preexec_fn=sandbox.builder_cage_preexec(
                [worktree, *extra], [common_dir, *read], network))


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
