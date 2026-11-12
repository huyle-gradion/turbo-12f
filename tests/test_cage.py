"""Tests for bin/t12-cage: the builder's cage (issue #4).

A builder runs inside the cage. It can write its worktree, but not the
shared product `.git` (where a hook, filter, replace ref, or attributes
line would run code in the orchestrator's later git calls) and not the
state home (where a .pth file in the verify home would run code in the
next verification). Its edits reach the run's branch through
`t12-cage commit`, run outside the cage, which refuses a worktree whose
`.git` pointer no longer matches what `prepare` created.
"""

import json
import os
import subprocess
import tempfile
import unittest

from t12 import sandbox

from tests.test_t12 import APP_V1, BIN, ROOT, task_dict

CAGE = os.path.join(ROOT, "bin", "t12-cage")

# Each vector is (name, path, mode, payload). Run inside the cage; it
# reports which writes landed. It never fails on a denial: denial is
# the expected outcome and the test reads the report.
PLANT = r"""
import json, sys
vectors = json.loads(sys.argv[1])
report = {}
for name, path, mode, payload in vectors:
    try:
        with open(path, mode) as fh:
            fh.write(payload)
        report[name] = "written"
    except OSError as exc:
        report[name] = "denied: " + exc.strerror
print(json.dumps(report))
"""


class CageCase(unittest.TestCase):
    """A disposable product repo with one prepared run, demo-1. Not a
    CliCase subclass: that would inherit and rerun all of its tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(self.repo)
        self._git(self.repo, "init", "-b", "main")
        self._git(self.repo, "config", "user.email", "test@example.com")
        self._git(self.repo, "config", "user.name", "Test")
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write(APP_V1)
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-m", "init")
        task_path = os.path.join(self.tmp.name, "task.json")
        with open(task_path, "w") as fh:
            json.dump(task_dict(), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        self.worktree = os.path.join(self.home, "worktrees", "demo-1")

    def _git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def t12(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [BIN, *args], capture_output=True, text=True, timeout=120, env=env
        )
        self.assertEqual(result.returncode, 0,
                         f"t12 {' '.join(args)}: {result.stdout} {result.stderr}")
        return result

    def write_worktree(self, rel, content):
        with open(os.path.join(self.worktree, rel), "w") as fh:
            fh.write(content)

    def cage(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [CAGE, *args], capture_output=True, text=True, timeout=120,
            env=env, cwd=self.worktree,
        )
        if expect is not None:
            self.assertEqual(
                result.returncode, expect,
                f"t12-cage {' '.join(args)} -> {result.returncode}: "
                f"{result.stdout} {result.stderr}",
            )
        return result

    def branch(self):
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        return json.loads(out)["branch"]

    def git_dir(self):
        return os.path.join(self.repo, ".git")


@unittest.skipUnless(sandbox.landlock_available(), "needs a Landlock kernel")
class TamperingVectorTest(CageCase):
    def test_each_vector_is_denied_and_worktree_write_succeeds(self):
        git_dir = self.git_dir()
        verify_home = os.path.join(self.home, "verify-home", "demo-1")
        # Parent dirs exist before the cage starts, so a denial can only
        # come from the cage, not from a missing directory.
        for d in (
            os.path.join(git_dir, "hooks"),
            os.path.join(git_dir, "refs", "replace"),
            os.path.join(git_dir, "info"),
            verify_home,
        ):
            os.makedirs(d, exist_ok=True)
        head = self._git(self.repo, "rev-parse", "HEAD")
        with open(os.path.join(git_dir, "config")) as fh:
            config_before = fh.read()

        vectors = [
            ("hook", os.path.join(git_dir, "hooks", "pre-commit"), "w",
             "#!/bin/sh\ntouch /tmp/pwned\n"),
            ("smudge-filter", os.path.join(git_dir, "config"), "a",
             '[filter "evil"]\n\tsmudge = touch /tmp/pwned\n'),
            ("replace-ref", os.path.join(git_dir, "refs", "replace", head), "w",
             head + "\n"),
            ("info-attributes", os.path.join(git_dir, "info", "attributes"), "a",
             "* filter=evil\n"),
            ("verify-home-pth", os.path.join(verify_home, "evil.pth"), "w",
             "import os; os.system('touch /tmp/pwned')\n"),
            ("worktree-file", os.path.join(self.worktree, "app.py"), "w",
             "BUILDER_EDIT = True\n"),
        ]
        result = self.cage(
            "--home", self.home, "--task-id", "demo-1", "--",
            "python3", "-c", PLANT, json.dumps(vectors),
        )
        report = json.loads(result.stdout.strip().splitlines()[-1])

        for name in ("hook", "smudge-filter", "replace-ref",
                     "info-attributes", "verify-home-pth"):
            self.assertTrue(report[name].startswith("denied"),
                            f"{name} was not denied: {report[name]}")
        self.assertEqual(report["worktree-file"], "written")

        # The filesystem agrees with the report.
        self.assertFalse(os.path.exists(os.path.join(git_dir, "hooks", "pre-commit")))
        with open(os.path.join(git_dir, "config")) as fh:
            self.assertEqual(fh.read(), config_before)
        self.assertFalse(os.path.exists(os.path.join(git_dir, "refs", "replace", head)))
        attributes = os.path.join(git_dir, "info", "attributes")
        if os.path.exists(attributes):
            with open(attributes) as fh:
                self.assertNotIn("filter=evil", fh.read())
        self.assertFalse(os.path.exists(os.path.join(verify_home, "evil.pth")))
        with open(os.path.join(self.worktree, "app.py")) as fh:
            self.assertEqual(fh.read(), "BUILDER_EDIT = True\n")


@unittest.skipIf(sandbox.landlock_available(), "a cage applies on this host")
class NoCageRefusalTest(CageCase):
    def test_refuses_to_start_without_a_cage(self):
        marker = os.path.join(self.worktree, "ran.txt")
        result = self.cage(
            "--home", self.home, "--task-id", "demo-1", "--",
            "python3", "-c", f"open({marker!r}, 'w').write('x')",
            expect=None,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(marker), "command ran without a cage")


class CageCommitTest(CageCase):
    def test_commit_lands_worktree_changes_on_the_run_branch(self):
        branch = self.branch()
        before = self._git(self.repo, "rev-parse", branch)
        self.write_worktree("app.py", "BUILDER_EDIT = True\n")

        self.cage("commit", "--home", self.home, "--task-id", "demo-1",
                  "--message", "feat: builder edit")

        after = self._git(self.repo, "rev-parse", branch)
        self.assertNotEqual(after, before)
        self.assertEqual(self._git(self.repo, "rev-parse", f"{after}^"), before)
        self.assertEqual(self._git(self.repo, "log", "-1", "--format=%s", after),
                         "feat: builder edit")
        self.assertEqual(self._git(self.repo, "show", f"{after}:app.py"),
                         "BUILDER_EDIT = True")

    def test_commit_refuses_a_changed_git_pointer(self):
        # A decoy repo the builder could redirect the worktree into.
        decoy = os.path.join(self.tmp.name, "decoy")
        os.makedirs(decoy)
        self._git(decoy, "init", "-b", "main")
        self._git(decoy, "config", "user.email", "test@example.com")
        self._git(decoy, "config", "user.name", "Test")
        self._git(decoy, "commit", "--allow-empty", "-m", "decoy")
        decoy_head = self._git(decoy, "rev-parse", "HEAD")

        branch = self.branch()
        before = self._git(self.repo, "rev-parse", branch)
        self.write_worktree("app.py", "BUILDER_EDIT = True\n")
        with open(os.path.join(self.worktree, ".git"), "w") as fh:
            fh.write(f"gitdir: {os.path.join(decoy, '.git')}\n")

        result = self.cage("commit", "--home", self.home, "--task-id", "demo-1",
                           "--message", "feat: builder edit", expect=None)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._git(self.repo, "rev-parse", branch), before)
        self.assertEqual(self._git(decoy, "rev-parse", "HEAD"), decoy_head)


if __name__ == "__main__":
    unittest.main()
