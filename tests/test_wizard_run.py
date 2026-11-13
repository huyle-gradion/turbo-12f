"""Tests for bin/t12-wizard `run`. A fake runner and a fake `t12 board` sit
in a temporary workspace; nothing touches the network, a socket, or the
real home directory.

Contract the tests pin down (the implementation must follow it):
- the workspace file t12-workspace.json carries `model_cmd`;
- `run` calls <work>/factory/adapters/runner.py with --home, --queue and
  --model-cmd taken from the workspace (the user passes none);
- while the runner is alive, `run` reads `<work>/factory/bin/t12 board
  --home <state> --json` and prints a progress line each interval; the
  interval in seconds comes from env T12_WIZARD_PROGRESS_SECS (default 60).
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIZARD = os.path.join(ROOT, "bin", "t12-wizard")

RUNNER = r'''#!{python}
import json, os, sys, time
with open(os.path.join(os.environ["FAKE_STATE"], "log.jsonl"), "a") as f:
    f.write(json.dumps({{"tool": "runner", "args": sys.argv[1:]}}) + "\n")
time.sleep(float(os.environ.get("FAKE_RUNNER_SLEEP", "0")))
if os.environ.get("FAKE_RUNNER_FAIL"):
    sys.stderr.write("gh-7: builder step failed: boom\n")
    sys.exit(2)
'''

T12 = r'''#!{python}
import os, sys
if sys.argv[1:2] == ["board"]:
    with open(os.path.join(os.environ["FAKE_STATE"], "board.json")) as f:
        sys.stdout.write(f.read())
    sys.exit(0)
sys.exit(1)
'''

ROW = {"task_id": "gh-7", "phase": "red", "next": "builder: stage", "busy": True}


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.work = os.path.join(self.tmp, "work")
        self.queue = os.path.join(self.work, "state", "queue")
        for d in (self.state, os.path.join(self.tmp, "home"), self.queue,
                  os.path.join(self.work, "factory", "adapters"),
                  os.path.join(self.work, "factory", "bin"),
                  os.path.join(self.work, "state", "runs")):
            os.makedirs(d)
        self.install(os.path.join(self.work, "factory", "adapters", "runner.py"), RUNNER)
        self.install(os.path.join(self.work, "factory", "bin", "t12"), T12)
        with open(os.path.join(self.work, "t12-workspace.json"), "w") as f:
            json.dump({"product_repo": "https://example.invalid/acme/shop.git",
                       "work_dir": self.work, "commit_name": "Ada",
                       "commit_email": "ada@example.com", "rules_branch": "main",
                       "model_cmd": "fake-model --flag"}, f)
        self.board([])

    @staticmethod
    def install(path, template):
        with open(path, "w") as f:
            f.write(template.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def board(self, rows):
        with open(os.path.join(self.state, "board.json"), "w") as f:
            json.dump(rows, f)

    def queue_task(self):
        with open(os.path.join(self.queue, "gh-7.json"), "w") as f:
            f.write("{}")

    def run_wizard(self, args, **env):
        e = {"PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin",
             "HOME": os.path.join(self.tmp, "home"), "FAKE_STATE": self.state,
             "GIT_ALLOW_PROTOCOL": "none"}
        e.update(env)
        r = subprocess.run([sys.executable, WIZARD] + args, cwd=self.work, env=e,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        return r, r.stdout + r.stderr

    def runner_calls(self):
        path = os.path.join(self.state, "log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line)["args"] for line in f]

    def test_run_uses_workspace_values_without_flags(self):
        self.queue_task()
        self.board([ROW])
        r, out = self.run_wizard(["run"])
        self.assertEqual(r.returncode, 0, out)
        calls = self.runner_calls()
        self.assertEqual(len(calls), 1, calls)
        a = calls[0]
        self.assertEqual(os.path.realpath(a[a.index("--home") + 1]), os.path.join(self.work, "state"))
        self.assertEqual(os.path.realpath(a[a.index("--queue") + 1]), self.queue)
        self.assertEqual(a[a.index("--model-cmd") + 1], "fake-model --flag")

    def test_run_prints_progress_while_a_step_runs(self):
        self.queue_task()
        self.board([ROW])
        r, out = self.run_wizard(["run"], FAKE_RUNNER_SLEEP="3.5",
                                 T12_WIZARD_PROGRESS_SECS="1")
        self.assertEqual(r.returncode, 0, out)
        lines = [ln for ln in out.splitlines() if "gh-7" in ln and "builder" in ln]
        self.assertGreaterEqual(len(lines), 2, out)
        for ln in lines:
            self.assertRegex(ln, r"\d+\s*(s\b|sec|second|m\b|min)", ln)

    def test_run_with_nothing_to_do_suggests_take(self):
        r, out = self.run_wizard(["run"])
        self.assertEqual(r.returncode, 0, out)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, out)
        self.assertIn("take", lines[0])

    def test_run_failure_says_what_failed_and_how_to_retry(self):
        self.queue_task()
        self.board([ROW])
        r, out = self.run_wizard(["run"], FAKE_RUNNER_FAIL="1")
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("boom", out)
        retries = [ln for ln in out.splitlines() if "t12-wizard run" in ln]
        self.assertEqual(len(retries), 1, out)


if __name__ == "__main__":
    unittest.main()
