"""Tests for delivery (Phase 7): the core records adapter reports,
pins them to the reviewed head, and blocks blind retries after an
uncertain outcome.
"""

import json
import os
import subprocess
import tempfile
import unittest

TEST_FILE = '''import unittest
from app import greet

class TestGreet(unittest.TestCase):
    def test_named(self):
        self.assertEqual(greet("Ana"), "Hello, Ana!")

    def test_default(self):
        self.assertEqual(greet(), "Hello, world!")
'''

APP_V2 = '''def greet(name="world"):
    return f"Hello, {name}!"
'''


def task_dict(task_id="demo-1"):
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
        "gates": [{"name": "unit", "command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]}],
    }


class DeliverCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.repo, "tests"))
        self._git(self.repo, "init", "-b", "main", "-q")
        self._git(self.repo, "config", "user.email", "t@example.com")
        self._git(self.repo, "config", "user.name", "T")
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write('def greet(name="world"):\n    return "Hello!"\n')
        open(os.path.join(self.repo, "tests", "__init__.py"), "w").close()
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-qm", "init")
        self.worktree = os.path.join(self.home, "worktrees", "demo-1")
        self.t12("init", "--repo", self.repo, "--home", self.home)
        task_path = os.path.join(self.tmp.name, "task.json")
        with open(task_path, "w") as fh:
            json.dump(task_dict(), fh)
        self.t12("prepare", "--home", self.home, "--task", task_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            ["python3", "-m", "t12.cli", *args],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, expect,
                         f"t12 {' '.join(args)} -> {result.returncode}: {result.stderr}")
        return result

    def run_to_verified(self):
        with open(os.path.join(self.worktree, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", self.home, "--task-id", "demo-1",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(self.worktree, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(self.worktree, "add", ".")
        self._git(self.worktree, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        report = os.path.join(self.tmp.name, "review.md")
        with open(report, "w") as fh:
            fh.write("Verdict: approve\n")
        self.t12("review", "--home", self.home, "--task-id", "demo-1",
                 "--role", "reviewer", "--session", "sess-reviewer",
                 "--verdict", "approve", "--report", report)
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            return json.load(fh)["candidate"]["commit"]

    def write_report(self, outcome, head, adapter="github", ref=None, task_id="demo-1",
                     detail="test report"):
        path = os.path.join(self.tmp.name, f"delivery-{adapter}.json")
        with open(path, "w") as fh:
            json.dump({"adapter": adapter, "task_id": task_id, "head": head,
                       "outcome": outcome, "ref": ref, "detail": detail}, fh)
        return path

    def test_deliver_requires_local_verified(self):
        candidate = "0" * 40
        path = self.write_report("confirmed", candidate)
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path, expect=1)
        self.assertIn("requires phase 'local-verified'", result.stderr)

    def test_deliver_records_confirmed(self):
        candidate = self.run_to_verified()
        path = self.write_report("confirmed", candidate, ref="https://example.invalid/pr/1")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path)
        self.assertIn("github confirmed", result.stdout)
        evidence = os.path.join(self.home, "evidence", "demo-1", "delivery-github-1.json")
        self.assertTrue(os.path.exists(evidence))
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        status = json.loads(out)
        self.assertEqual(len(status["deliveries"]), 1)
        self.assertEqual(status["deliveries"][0]["outcome"], "confirmed")
        self.assertEqual(status["deliveries"][0]["ref"], "https://example.invalid/pr/1")
        self.assertEqual(status["deliveries"][0]["head"], candidate)
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(result.returncode, 0)

    def test_deliver_pins_head(self):
        candidate = self.run_to_verified()
        path = self.write_report("confirmed", "f" * 40)
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path, expect=1)
        self.assertIn("pinned to the reviewed head", result.stderr)
        self.assertNotEqual(candidate, "f" * 40)

    def test_deliver_rejects_bad_reports(self):
        candidate = self.run_to_verified()
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", bad, expect=1)
        self.assertIn("not valid JSON", result.stderr)
        path = self.write_report("maybe", candidate)
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path, expect=1)
        self.assertIn("unknown outcome", result.stderr)
        path = self.write_report("confirmed", candidate, adapter="asana")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path, expect=1)
        self.assertIn("for adapter 'asana', not 'github'", result.stderr)
        path = self.write_report("confirmed", candidate, task_id="other-task")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path, expect=1)
        self.assertIn("for task 'other-task'", result.stderr)

    def test_uncertain_blocks_and_resolves(self):
        candidate = self.run_to_verified()
        path = self.write_report("uncertain", candidate, detail="timeout during create")
        self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                 "--adapter", "github", "--report", path)
        path2 = self.write_report("confirmed", candidate, ref="https://example.invalid/pr/2")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path2, expect=1)
        self.assertIn("blocked", result.stderr)
        self.assertIn("uncertain", result.stderr)
        # A resolving report (a read-back, not a new action) unblocks it.
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path2, "--resolves")
        self.assertIn("github confirmed", result.stdout)
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        self.assertEqual(len(json.loads(out)["deliveries"]), 2)
        # The block is per adapter: asana is unaffected.
        path3 = self.write_report("confirmed", candidate, adapter="asana")
        self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                 "--adapter", "asana", "--report", path3)

    def test_verify_catches_tampered_delivery_report(self):
        candidate = self.run_to_verified()
        path = self.write_report("confirmed", candidate)
        self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                 "--adapter", "github", "--report", path)
        evidence = os.path.join(self.home, "evidence", "demo-1", "delivery-github-1.json")
        with open(evidence, "a") as fh:
            fh.write("forged\n")
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("FAIL evidence-refs", result.stdout + result.stderr)

    def test_deliver_refuses_moved_head(self):
        candidate = self.run_to_verified()
        with open(os.path.join(self.worktree, "app.py"), "a") as fh:
            fh.write("# moved\n")
        self._git(self.worktree, "add", ".")
        self._git(self.worktree, "commit", "-m", "chore: move head")
        path = self.write_report("confirmed", candidate)
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", path, expect=1)
        self.assertIn("HEAD moved", result.stderr)


if __name__ == "__main__":
    unittest.main()
