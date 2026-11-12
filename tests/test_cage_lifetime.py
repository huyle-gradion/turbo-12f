"""Tests for what bin/t12-cage leaves behind (issue #4).

`t12-cage commit` checks the worktree's `.git` pointer once. A caged
process that outlived the cage could rewrite the pointer after that
check, so the cage returns only when every process it started is dead.
"""

import os
import signal
import subprocess
import time
import unittest
from unittest import mock

from t12 import sandbox

from tests.test_cage import CAGE, CageCase

# Leaves the cage's session and process group, then keeps appending
# to a worktree file. Prints the detached pid before the cage's
# command exits.
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
os.write(write_end, str(os.getpid()).encode())
os.close(write_end)
while True:
    with open(beat, "a") as fh:
        fh.write("x")
    time.sleep(0.05)
"""


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
