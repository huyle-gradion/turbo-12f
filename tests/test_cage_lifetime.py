"""Tests for what bin/t12-cage leaves behind (issue #4).

`t12-cage commit` checks the worktree's `.git` pointer once. A caged
process that outlived the cage could rewrite the pointer after that
check, so no caged process outlives `t12-cage`, however it ends.
"""

import os
import resource
import signal
import subprocess
import time
import unittest
from unittest import mock

from t12 import sandbox

from tests.test_cage import CAGE, CageCase

# Leaves the cage's session and process group, then keeps appending
# to a worktree file. Prints the detached pid before the cage's
# command exits. /proc is the host's, so /proc/self names the pid the
# test can look up, which os.getpid() inside a PID namespace does not.
DETACH = r"""
import os, sys, time
beat = sys.argv[1]
read_end, write_end = os.pipe()
if os.fork():
    os.close(write_end)
    print(os.read(read_end, 32).decode(), flush=True)
    sys.exit(0)
os.setsid()
if os.fork():
    os._exit(0)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
os.write(write_end, os.readlink("/proc/self").encode())
os.close(write_end)
while True:
    with open(beat, "a") as fh:
        fh.write("x")
    time.sleep(0.05)
"""

# Detaches like DETACH, records its pid in a worktree file, attacks the
# process that started the command, then rewrites the worktree's .git
# pointer after a delay. The command itself exits at once, or stays
# for a while when the attack is "none".
REWRITE_LATER = r"""
import os, resource, sys, time
attack, delay, pid_file = sys.argv[1], float(sys.argv[2]), sys.argv[3]
parent = os.getppid()
read_end, write_end = os.pipe()
if os.fork():
    os.close(write_end)
    os.read(read_end, 1)
    if attack == "none":
        time.sleep(delay + 2)
    sys.exit(0)
os.setsid()
if os.fork():
    os._exit(0)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
with open(pid_file + ".tmp", "w") as fh:
    fh.write(os.readlink("/proc/self"))
os.rename(pid_file + ".tmp", pid_file)
if attack != "none":
    try:
        resource.prlimit(parent, int(attack), (0, 0))
    except OSError:
        pass
os.close(write_end)
time.sleep(delay)
with open(".git", "w") as fh:
    fh.write("gitdir: /tmp/attacker\n")
"""
REWRITE_DELAY_S = 1.0


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


@unittest.skipUnless(sandbox.landlock_available(), "needs a Landlock kernel")
class CageLifetimeTest(CageCase):
    def detach(self, beat):
        return ["--home", self.home, "--task-id", "demo-1", "--",
                "python3", "-c", DETACH, beat]

    def assert_silent(self, pid, beat):
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertFalse(alive(pid), "a detached process outlived the cage")
        size = os.path.getsize(beat)
        time.sleep(0.3)
        self.assertEqual(os.path.getsize(beat), size,
                         "the worktree was written after the cage returned")

    def test_detached_process_is_dead_when_the_cage_returns(self):
        beat = os.path.join(self.worktree, "beat.txt")
        result = self.cage(*self.detach(beat))
        pid = int(result.stdout.strip().splitlines()[-1])

        self.assertGreater(os.path.getsize(beat), 0, "the detached process never ran")
        self.assert_silent(pid, beat)

    def test_detached_process_is_dead_when_the_cage_is_terminated(self):
        beat = os.path.join(self.worktree, "beat.txt")
        hold = "import subprocess, sys, time; " \
               "subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]]); " \
               "time.sleep(60)"
        proc = subprocess.Popen(
            [CAGE, "--home", self.home, "--task-id", "demo-1", "--",
             "python3", "-c", hold, DETACH, beat],
            stdout=subprocess.PIPE, text=True, cwd=self.worktree)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        pid = int(proc.stdout.readline())

        proc.terminate()
        self.assertEqual(proc.wait(timeout=30), 128 + signal.SIGTERM)

        self.assert_silent(pid, beat)

    def read_pointer(self):
        with open(os.path.join(self.worktree, ".git")) as fh:
            return fh.read()

    def start_rewriter(self, attack):
        """Start t12-cage on REWRITE_LATER and return (t12-cage, the
        detached process's pid)."""
        pid_file = os.path.join(self.worktree, "rewriter.pid")
        cage = subprocess.Popen(
            [CAGE, "--home", self.home, "--task-id", "demo-1", "--",
             "python3", "-c", REWRITE_LATER, str(attack), str(REWRITE_DELAY_S), pid_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.worktree,
            # A killed t12-cage leaves its private temp dir behind.
            env=dict(os.environ, TMPDIR=self.tmp.name))
        self.addCleanup(cage.wait)
        self.addCleanup(cage.kill)
        deadline = time.monotonic() + 30
        while not os.path.exists(pid_file):
            self.assertLess(time.monotonic(), deadline, "the detached process never started")
            time.sleep(0.01)
        with open(pid_file) as fh:
            pid = int(fh.read())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        return cage, pid

    def assert_pointer_survives(self, pointer, pid):
        """The detached process is gone before it could rewrite the
        pointer. Give it the time it needs to rewrite, so a process
        that did outlive the cage shows in the pointer too."""
        deadline = time.monotonic() + REWRITE_DELAY_S + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.read_pointer(), pointer,
                         "a process that outlived the cage rewrote the .git pointer")
        self.assertFalse(alive(pid), "a detached process outlived the cage")

    def assert_limit_on_the_parent_leaves_no_survivor(self, limit):
        pointer = self.read_pointer()
        cage, pid = self.start_rewriter(limit)

        cage.wait(timeout=30)

        self.assert_pointer_survives(pointer, pid)

    def test_cpu_limit_on_the_parent_leaves_no_survivor(self):
        # A zero CPU limit makes the kernel kill the process it is set on.
        self.assert_limit_on_the_parent_leaves_no_survivor(resource.RLIMIT_CPU)

    def test_open_file_limit_on_the_parent_leaves_no_survivor(self):
        # A zero open-file limit fails any cleanup that opens a file.
        self.assert_limit_on_the_parent_leaves_no_survivor(resource.RLIMIT_NOFILE)

    def test_killing_the_cage_leaves_no_survivor(self):
        pointer = self.read_pointer()
        cage, pid = self.start_rewriter("none")

        cage.kill()
        cage.wait(timeout=30)

        self.assert_pointer_survives(pointer, pid)

    def test_git_status_works_despite_an_unreadable_user_config(self):
        # A user exclude file the cage may not read: git dies on it.
        user_home = os.path.join(self.tmp.name, "user")
        os.makedirs(os.path.join(user_home, ".config", "git"))
        with open(os.path.join(user_home, ".config", "git", "ignore"), "w") as fh:
            fh.write("*.log\n")
        env = mock.patch.dict(os.environ, HOME=user_home)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)
        self.write_worktree("app.py", "BUILDER_EDIT = True\n")

        result = self.cage(
            "--home", self.home, "--task-id", "demo-1", "--",
            "sh", "-c", 'git status --porcelain && test -d "$XDG_CONFIG_HOME"')

        self.assertEqual(result.stdout.strip(), "M app.py")


if __name__ == "__main__":
    unittest.main()
