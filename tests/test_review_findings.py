"""gh-46: after a changes-requested review the builder sees the review,
and re-staging the very commit that was reviewed is refused instead of
looping review forever."""

import json
import os
import subprocess
import tempfile
import unittest

from tests.test_t12 import APP_V1, APP_V2, BIN, COVERS, TEST_FILE, task_dict

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
                 "--marker", "FAILED", "--session", "sess-test-writer", "--covers", COVERS)
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

    def run_record(self):
        path = os.path.join(self.home, "runs", "demo-1.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def events(self):
        path = os.path.join(self.home, "runs", "demo-1.events.jsonl")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def stage(self, expect):
        return self.t12("stage", "--home", self.home, "--task-id", "demo-1",
                        "--session", "sess-builder", expect=expect)

    def test_refused_restage_pauses_with_the_report_in_the_question(self):
        report = (f"Verdict: changes-requested\n\n## Findings\n\n- {FINDING}\n")
        self.stage(expect=1)
        record = self.run_record()
        self.assertEqual(record["phase"], "paused")
        open_questions = [e for e in record["escalations"] if e["answer"] is None]
        self.assertEqual(len(open_questions), 1)
        self.assertIn(report, open_questions[0]["question"])
        self.assertIn("stage-refused", [e.get("type") or e.get("event")
                                        for e in self.events()])

    def test_uncommitted_refusal_leaves_phase_and_escalations_alone(self):
        wt = os.path.join(self.home, "worktrees", "demo-1")
        self.write(wt, "scratch.txt", "dirty\n")
        phase = self.status()["phase"]
        self.stage(expect=1)
        record = self.run_record()
        self.assertEqual(record["phase"], phase)
        self.assertEqual(record["escalations"], [])
        os.remove(os.path.join(wt, "scratch.txt"))
        # The different refusal must not be mistaken for the escalating one:
        # once clean, the reviewed-commit refusal still escalates.
        self.stage(expect=1)
        self.assertEqual(self.run_record()["phase"], "paused")

    def test_answered_escalation_then_new_commit_stages_for_one_op(self):
        self.stage(expect=1)
        after = self.status()["budgets"]
        self.t12("resume", "--home", self.home, "--task-id", "demo-1",
                 "--answer", "fix the newline contract")
        wt = os.path.join(self.home, "worktrees", "demo-1")
        self.write(wt, "app.py", APP_V2 + "\n# address review\n")
        self.git(wt, "add", ".")
        self.git(wt, "commit", "-m", "fix: address review")
        new_commit = self.git(wt, "rev-parse", "HEAD")
        self.stage(expect=0)
        status = self.status()
        self.assertEqual(status["candidate"], new_commit)
        self.assertEqual(status["phase"], "gates")
        self.assertEqual(status["budgets"]["ops_used"], after["ops_used"] + 1)
        self.assertEqual(status["budgets"]["repairs_used"], after["repairs_used"])


if __name__ == "__main__":
    unittest.main()
