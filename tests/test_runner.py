"""adapters/runner.py against real runs, with tests/fake_model.py as the
model: no network, no model calls."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T12 = os.path.join(ROOT, "bin", "t12")
RUNNER = os.path.join(ROOT, "adapters", "runner.py")
FAKE = os.path.join(ROOT, "tests", "fake_model.py")


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        join = lambda *p: os.path.join(self.tmp.name, *p)
        self.repo, self.home, self.queue = join("product"), join("state"), join("queue")
        self.log, self.notes = join("model.log"), join("notes.log")
        os.makedirs(os.path.join(self.repo, "tests"))
        os.makedirs(self.queue)
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write('def greet(name="world"):\n    return "Hello!"\n')
        open(os.path.join(self.repo, "tests", "__init__.py"), "w").close()
        for cmd in (["init", "-qb", "main"], ["config", "user.email", "t@example.com"],
                    ["config", "user.name", "T"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        subprocess.run([T12, "init", "--repo", self.repo, "--home", self.home],
                       check=True, capture_output=True)
        with open(os.path.join(self.queue, "demo-1.json"), "w") as fh:
            json.dump({
                "id": "demo-1", "title": "greet by name", "base": "main",
                "acceptance_criteria": ["greet('Ana') returns 'Hello, Ana!'"],
                "time_limit_s": 600, "op_limit": 50, "repair_cap": 2,
                "gates": [{"name": "unit",
                           "command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"]}],
            }, fh)

    def set_gate(self, command):
        path = os.path.join(self.queue, "demo-1.json")
        with open(path) as fh:
            task = json.load(fh)
        task["gates"] = [{"name": "unit", "command": command}]
        with open(path, "w") as fh:
            json.dump(task, fh)

    def drive(self, mode="", expect=0):
        env = dict(os.environ, FAKE_MODE=mode, FAKE_LOG=self.log, PYTHONDONTWRITEBYTECODE="1")
        notify = f"{sys.executable} -c 'import sys; open(sys.argv[1], \"a\").write(sys.argv[2] + \"\\n\")' {self.notes}"
        result = subprocess.run(
            [sys.executable, RUNNER, "--home", self.home, "--queue", self.queue, "--max", "1",
             "--model-cmd", f"{sys.executable} {FAKE}", "--notify-cmd", notify],
            capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stdout

    def run_record(self):
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            return json.load(fh)

    def lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def calls(self):
        return [json.loads(line) for line in self.lines(self.log)]

    def test_a_queued_task_reaches_local_verified_with_one_fresh_session_per_worker_call(self):
        self.drive()
        run = self.run_record()
        self.assertEqual(run["phase"], "local-verified")
        calls = self.calls()
        self.assertEqual([c["role"] for c in calls], ["test-writer", "builder", "reviewer"])
        sessions = [b["session"] for b in run["bindings"]]
        self.assertEqual(len(set(sessions)), len(sessions), run["bindings"])
        self.assertEqual({b["role"] for b in run["bindings"]},
                         {"test-writer", "builder", "qa", "reviewer"})
        self.assertEqual(self.lines(self.notes), ["waiting demo-1: human: merge"])

    def test_an_escalation_is_notified_and_never_answered(self):
        self.drive(mode="escalate")
        run = self.run_record()
        self.assertEqual(run["phase"], "paused")
        self.assertIsNone(run["escalations"][0]["answer"])
        self.assertEqual(self.lines(self.notes),
                         ["waiting demo-1: human: answer 'Which greeting does the product want?'"])
        self.drive()
        self.assertEqual(self.run_record()["phase"], "paused")
        self.assertEqual(len(self.calls()), 1)

    def test_a_rejected_output_goes_back_once_to_the_same_session(self):
        self.drive(mode="garbage-once")
        self.assertEqual(self.run_record()["phase"], "local-verified")
        builder = [c for c in self.calls() if c["role"] == "builder"]
        self.assertEqual([c["rejected"] for c in builder], [False, True])
        self.assertEqual(builder[0]["session"], builder[1]["session"])

    def test_a_restarted_runner_carries_on_from_the_run_record(self):
        out = self.drive(mode="builder-fails")
        self.assertIn("builder model call failed", out)
        self.assertEqual(self.run_record()["phase"], "red")
        self.drive()
        self.assertEqual(self.run_record()["phase"], "local-verified")
        roles = [c["role"] for c in self.calls()]
        self.assertEqual(roles, ["test-writer", "builder", "builder", "reviewer"])

    def test_a_step_the_factory_refuses_is_not_retried_in_the_same_invocation(self):
        self.drive(mode="no-commit")
        run = self.run_record()
        self.assertEqual(run["phase"], "red")
        self.assertEqual([c["role"] for c in self.calls()], ["test-writer", "builder"])
        [note] = self.lines(self.notes)
        self.assertIn("waiting demo-1: builder: stage did not move the run", note)
        self.assertIn("uncommitted changes", note)

    def test_a_failing_replay_runs_once_per_invocation(self):
        self.set_gate(["true"])
        self.drive(mode="wrong-fix")
        events = self.lines(os.path.join(self.home, "runs", "demo-1.events.jsonl"))
        self.assertEqual(sum('"replay-failed"' in e for e in events), 1)
        [note] = self.lines(self.notes)
        self.assertIn("waiting demo-1: replay did not move the run", note)

    def test_a_second_runner_on_the_same_home_is_refused(self):
        import fcntl
        with open(os.path.join(self.home, "runner.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.drive(expect=1)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
