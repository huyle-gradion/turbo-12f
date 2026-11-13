"""gh-53: a builder escalation after committing must stay resumable."""

import json
import time
import unittest
from unittest import mock

from t12 import cli, model
from tests import test_t12 as base


class EscalateResumeCase(base.CliCase):
    def commit_then_escalate(self):
        self.do_red()
        self.write_worktree("app.py", base.APP_V2)
        self.commit_worktree("feat: implement greet")
        return self.t12("escalate", "--home", self.home, "--task-id", "demo-1",
                        "--question", "is this the right approach?", expect=None)

    def t12(self, *args, expect=0, extra_env=None):
        if expect is None:
            import os, subprocess
            return subprocess.run([base.BIN, *args], capture_output=True, text=True,
                                  timeout=120, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        return super().t12(*args, expect=expect, extra_env=extra_env)

    def test_escalation_after_commit_restages_or_is_refused_with_stage_hint(self):
        result = self.commit_then_escalate()
        if result.returncode != 0:
            self.assertIn("stage", result.stderr)
            self.assertNotEqual(self.phase(), "paused")
        else:
            status = json.loads(self.t12(
                "status", "--home", self.home, "--task-id", "demo-1").stdout)
            head = self._git(self.worktree, "rev-parse", "HEAD")
            self.assertEqual(status["candidate"], head)

    def escalate_then_resume(self):
        result = self.commit_then_escalate()
        if result.returncode != 0:
            self.t12("stage", "--home", self.home, "--task-id", "demo-1",
                     "--session", "sess-builder")
            self.t12("escalate", "--home", self.home, "--task-id", "demo-1",
                     "--question", "is this the right approach?")
        self.t12("resume", "--home", self.home, "--task-id", "demo-1", "--answer", "yes")
        self.assertNotEqual(self.phase(), "paused")

    def test_resume_after_escalation_with_commit_succeeds(self):
        self.escalate_then_resume()

    def test_resume_still_refuses_when_head_moved_without_a_recorded_stage(self):
        self.escalate_then_resume()
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "x")
        self.write_worktree("app.py", base.APP_V1)
        self.commit_worktree("feat: unstaged work")
        result = self.t12("resume", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("HEAD moved", result.stderr)

    def test_paused_time_does_not_count_against_the_time_budget(self):
        self.do_red()
        now = time.time()
        with mock.patch("time.time", return_value=now):
            self.assertEqual(cli.main(["pause", "--home", self.home,
                                       "--task-id", "demo-1", "--reason", "away"]), 0)
        with mock.patch("time.time", return_value=now + 100000):
            self.assertEqual(cli.main(["resume", "--home", self.home,
                                       "--task-id", "demo-1"]), 0)
        with mock.patch("time.time", return_value=now + 100010):
            run = model.load_run(self.home, "demo-1")
            model.check_budgets(run)
            self.assertGreater(model.time_left_s(run), 400)


if __name__ == "__main__":
    unittest.main()
