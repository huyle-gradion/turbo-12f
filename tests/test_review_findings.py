"""gh-46: after a changes-requested review the builder sees the review,
and re-staging the very commit that was reviewed is refused instead of
looping review forever."""

import json
import os
import subprocess
import tempfile
import unittest

from tests.test_t12 import APP_V1, APP_V2, BIN, TEST_FILE, task_dict

FINDING = "greet() drops the trailing newline contract"


class ChangesRequestedLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(repo, "tests"))
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.email", "test@example.com")
        self.git(repo, "config", "user.name", "Test")
        self.write(repo, "app.py", APP_V1)
        self.write(repo, "tests/__init__.py", "")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "init")
        task_path = os.path.join(self.tmp.name, "task.json")
        with open(task_path, "w") as fh:
            json.dump(task_dict(), fh)
        self.t12("init", "--repo", repo, "--home", self.home)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        wt = os.path.join(self.home, "worktrees", "demo-1")

        self.write(wt, "tests/test_greet.py", TEST_FILE)
        self.t12("red", "--home", self.home, "--task-id", "demo-1",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        self.write(wt, "app.py", APP_V2)
        self.git(wt, "add", ".")
        self.git(wt, "commit", "-m", "feat: implement greet")
        self.commit = self.git(wt, "rev-parse", "HEAD")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1",
                 "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        report = os.path.join(self.tmp.name, "review.md")
        with open(report, "w") as fh:
            fh.write(f"Verdict: changes-requested\n\n## Findings\n\n- {FINDING}\n")
        self.t12("review", "--home", self.home, "--task-id", "demo-1",
                 "--role", "reviewer", "--session", "sess-reviewer",
                 "--verdict", "changes-requested", "--report", report)

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, root, rel, content):
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(content)

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([BIN, *args], capture_output=True, text=True,
                                timeout=120, env=env)
        self.assertEqual(result.returncode, expect,
                         f"t12 {' '.join(args)}: {result.stdout} {result.stderr}")
        return result

    def status(self):
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        return json.loads(out)

    def test_builder_context_carries_the_changes_requested_review(self):
        out = self.t12("context", "--home", self.home, "--task-id", "demo-1",
                       "--role", "builder", "--phase", "green").stdout
        context = json.loads(out.split("--- context (JSON) ---\n", 1)[1])
        review = context["prior_review"]
        self.assertIsNotNone(review, "builder context has no prior_review")
        self.assertEqual(review["verdict"], "changes-requested")
        self.assertEqual(review["commit"], self.commit)
        blob = json.dumps(review)
        self.assertTrue(FINDING in blob or "review-reviewer-1.md" in blob,
                        f"neither the findings nor their report are in {review}")

    def test_restaging_the_reviewed_commit_is_refused_for_one_op(self):
        before = self.status()["budgets"]["ops_used"]
        result = self.t12("stage", "--home", self.home, "--task-id", "demo-1",
                          "--session", "sess-builder", expect=1)
        message = result.stderr.lower()
        self.assertIn("changes", message)
        self.assertIn("no new commit", message)
        after = self.status()
        self.assertEqual(after["budgets"]["ops_used"], before + 1)
        self.assertEqual(after["candidate"], self.commit)


if __name__ == "__main__":
    unittest.main()
