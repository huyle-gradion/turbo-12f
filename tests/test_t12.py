"""Tests for turbo-12f. Synthetic git repos only: no network, no model,
no credentials. Each test builds a disposable product repo and drives
the real CLI end to end.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "t12")

APP_V1 = 'def greet(name="world"):\n    return "Hello!"\n'
APP_V2 = 'def greet(name="world"):\n    return f"Hello, {name}!"\n'
APP_WRONG = 'def greet(name="world"):\n    return f"Hello {name}"\n'
TEST_FILE = """import unittest
from app import greet

class TestGreet(unittest.TestCase):
    def test_named(self):
        self.assertEqual(greet("Ana"), "Hello, Ana!")

    def test_default(self):
        self.assertEqual(greet(), "Hello, world!")

if __name__ == "__main__":
    unittest.main()
"""


def task_dict(task_id="demo-1", gates=None):
    return {
        "id": task_id,
        "title": "Add greet()",
        "acceptance_criteria": [
            "greet('Ana') returns 'Hello, Ana!'",
            "greet() with no argument returns 'Hello, world!'",
        ],
        "base": "main",
        "time_limit_s": 600,
        "op_limit": 50,
        "repair_cap": 2,
        "gates": gates
        or [{"name": "unit", "command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]}],
    }


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(self.repo)
        self._git(self.repo, "init", "-b", "main")
        self._git(self.repo, "config", "user.email", "test@example.com")
        self._git(self.repo, "config", "user.name", "Test")
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write(APP_V1)
        os.makedirs(os.path.join(self.repo, "tests"))
        with open(os.path.join(self.repo, "tests", "__init__.py"), "w") as fh:
            fh.write("")
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-m", "init")
        self.task_path = os.path.join(self.tmp.name, "task.json")
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        self.t12("prepare", "--home", self.home, "--task", self.task_path)
        self.worktree = os.path.join(self.home, "worktrees", "demo-1")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [BIN, *args], capture_output=True, text=True, timeout=120, env=env
        )
        self.assertEqual(
            result.returncode, expect,
            f"t12 {' '.join(args)} -> {result.returncode}: {result.stdout} {result.stderr}",
        )
        return result

    def phase(self):
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        return json.loads(out)["phase"]

    def write_worktree(self, rel, content):
        path = os.path.join(self.worktree, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def commit_worktree(self, message):
        self._git(self.worktree, "add", ".")
        self._git(self.worktree, "commit", "-m", message)

    def do_red(self):
        self.write_worktree("tests/test_greet.py", TEST_FILE)
        self.t12(
            "red", "--home", self.home, "--task-id", "demo-1",
            "--test-path", "tests/test_greet.py",
            "--command", "python3 -m unittest tests.test_greet -q",
            "--marker", "FAILED",
        )
        self.assertEqual(self.phase(), "red")

    def do_implement_and_stage(self):
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: implement greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "gates")

    def test_happy_path_to_local_verified(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "green")
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")
        manifest = os.path.join(self.home, "evidence", "demo-1", "manifest.txt")
        self.assertTrue(os.path.exists(manifest))
        with open(manifest) as fh:
            text = fh.read()
        self.assertIn("local-verified", text)
        self.assertIn("unit: passed", text)
        # Terminal success cannot be stopped.
        self.t12("stop", "--home", self.home, "--task-id", "demo-1",
                 "--reason", "too late", expect=1)
        self.assertEqual(self.phase(), "local-verified")

    def test_green_without_replay_is_refused(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertEqual(self.phase(), "gates")

    def test_gate_order_is_enforced(self):
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(task_id="two-gates", gates=[
                {"name": "first", "command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]},
                {"name": "second", "command": ["python3", "-c", "pass"]},
            ]), fh)
        home2 = os.path.join(self.tmp.name, "state2")
        self.t12("init", "--repo", self.repo, "--home", home2)
        self.t12("prepare", "--home", home2, "--task", self.task_path)
        wt = os.path.join(home2, "worktrees", "two-gates")
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home2, "--task-id", "two-gates",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", home2, "--task-id", "two-gates")
        result = self.t12("gate", "--home", home2, "--task-id", "two-gates",
                          "--name", "second", expect=1)
        self.assertIn("cannot run before gate 'first'", result.stderr)

    def test_tampered_evidence_blocks_green(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        red_out = os.path.join(self.home, "evidence", "demo-1", "red-output.txt")
        with open(red_out, "a") as fh:
            fh.write("tampered\n")
        result = self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("evidence changed", result.stderr)
        self.assertEqual(self.phase(), "green")

    def test_passing_test_cannot_be_red(self):
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: implement greet first")
        # worktree is now ahead of base; red must fail because the test passes.
        # Reset to base state for a clean red attempt: use a fresh run instead.
        self.write_worktree("tests/test_greet.py", TEST_FILE)
        result = self.t12(
            "red", "--home", self.home, "--task-id", "demo-1",
            "--test-path", "tests/test_greet.py",
            "--command", "python3 -m unittest tests.test_greet -q",
            "--marker", "FAILED", expect=1,
        )
        self.assertIn("did not fail", result.stderr)
        self.assertEqual(self.phase(), "ready")

    def test_pause_resume_and_resume_drift(self):
        self.do_red()
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "lunch")
        self.assertEqual(self.phase(), "paused")
        self.t12("resume", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "red")
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "again")
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: drift while paused")
        result = self.t12("resume", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("HEAD moved while paused", result.stderr)

    def test_stop_from_paused(self):
        self.do_red()
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "lunch")
        self.t12("stop", "--home", self.home, "--task-id", "demo-1", "--reason", "abandoned")
        self.assertEqual(self.phase(), "stopped")
        status = json.loads(self.t12(
            "status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(status["stop_reason"], "abandoned")
        # A stopped run cannot move again.
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", expect=1)

    def test_supersede_active_run_refused(self):
        self.do_red()
        result = self.t12("prepare", "--home", self.home, "--task", self.task_path,
                          "--supersede", expect=1)
        self.assertIn("stop it first", result.stderr)
        self.assertEqual(self.phase(), "red")

    def test_stop_then_supersede_and_rerun(self):
        self.do_red()
        self.t12("stop", "--home", self.home, "--task-id", "demo-1", "--reason", "bad approach")
        self.assertEqual(self.phase(), "stopped")
        # Plain prepare refuses to overwrite.
        self.t12("prepare", "--home", self.home, "--task", self.task_path, expect=1)
        # Supersede archives the old run and starts fresh.
        self.t12("prepare", "--home", self.home, "--task", self.task_path, "--supersede")
        self.assertEqual(self.phase(), "ready")
        archived = [f for f in os.listdir(os.path.join(self.home, "runs"))
                    if "superseded" in f and f.endswith(".json")
                    and not f.endswith(".events.jsonl")]
        self.assertTrue(archived, "old run was not archived")
        with open(os.path.join(self.home, "runs", archived[0])) as fh:
            old = json.load(fh)
        self.assertEqual(old["phase"], "stopped")
        # The fresh run records what it superseded, and can go all the way.
        status = json.loads(self.t12(
            "status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(status["phase"], "ready")
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")

    def test_resume_refuses_tampered_evidence(self):
        self.do_red()
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "lunch")
        red_out = os.path.join(self.home, "evidence", "demo-1", "red-output.txt")
        with open(red_out, "a") as fh:
            fh.write("tampered while paused\n")
        result = self.t12("resume", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("evidence changed", result.stderr)
        self.assertEqual(self.phase(), "paused")

    def test_gate_failure_counts_repair_and_restage_resets(self):
        self.do_red()
        self.write_worktree("app.py", APP_WRONG)  # close, but still failing
        self.commit_worktree("feat: wrong implementation")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1")
        result = self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                          "--name", "unit", expect=1)
        self.assertIn("failed", result.stderr)
        status = json.loads(self.t12(
            "status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(status["budgets"]["repairs_used"], 1)
        # Fix, re-stage: gate observations reset, candidate re-pinned.
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: fix greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")


if __name__ == "__main__":
    unittest.main()
