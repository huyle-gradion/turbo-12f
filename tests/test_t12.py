"""Tests for turbo-12f. Synthetic git repos only: no network, no model,
no credentials. Each test builds a disposable product repo and drives
the real CLI end to end.
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from t12 import sandbox

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

    def t12(self, *args, expect=0, extra_env=None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if extra_env:
            env.update(extra_env)
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
            "--marker", "FAILED", "--session", "sess-test-writer",
        )
        self.assertEqual(self.phase(), "red")

    def do_implement_and_stage(self):
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: implement greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        self.assertEqual(self.phase(), "gates")

    def do_review(self, home=None, task_id="demo-1", verdict="approve",
                  session="sess-reviewer", role="reviewer"):
        home = home or self.home
        report = os.path.join(self.tmp.name, f"review-{task_id}-{role}.md")
        with open(report, "w") as fh:
            fh.write(f"Verdict: {verdict}\n\n## Findings\n\nNone. Hypotheses checked against the diff; verification re-run.\n")
        self.t12("review", "--home", home, "--task-id", task_id,
                 "--role", role, "--session", session,
                 "--verdict", verdict, "--report", report)

    def test_happy_path_to_local_verified(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "green")
        self.do_review()
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
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", home2, "--task-id", "two-gates", "--session", "sess-builder")
        result = self.t12("gate", "--home", home2, "--task-id", "two-gates",
                          "--name", "second", "--session", "sess-qa", expect=1)
        self.assertIn("cannot run before gate 'first'", result.stderr)

    def test_tampered_evidence_blocks_green(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
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
            "--marker", "FAILED", "--session", "sess-test-writer", expect=1,
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
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder", expect=1)

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
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
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

    def test_probe_command_passes(self):
        result = self.t12("probe", "--home", self.home, "--task-id", "demo-1")
        self.assertIn("PASS forbidden_credential_access", result.stdout)
        self.assertIn("PASS external_effect_restriction", result.stdout)
        self.assertIn("sandbox adapter: landlock", result.stdout)
        status = json.loads(self.t12(
            "status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(status["sandbox_adapter"], "landlock")
        self.assertTrue(all(p["passed"] for p in status["probes"]))

    def test_prepare_rejects_forbidden_gate_at_admission(self):
        task = {
            "id": "demo-2", "title": "bad gate", "base": "main",
            "acceptance_criteria": ["x"],
            "gates": [{"name": "unit", "order": 1,
                       "command": ["agy", "--dangerously-skip-permissions"]}],
        }
        task_path = os.path.join(self.tmp.name, "task2.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        result = self.t12("prepare", "--home", self.home, "--task", task_path, expect=1)
        self.assertIn("rejected at admission", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "runs", "demo-2.json")))

    def test_require_probes_blocks_red_when_probes_failed(self):
        with open(self.task_path) as fh:
            task = json.load(fh)
        task["id"] = "demo-rp"
        task["require_probes"] = True
        task_path = os.path.join(self.tmp.name, "task-rp.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        home2 = os.path.join(self.tmp.name, "home2")
        self.t12("init", "--home", home2, "--repo", self.repo)
        self.t12("prepare", "--home", home2, "--task", task_path)
        # Simulate a failed probe by editing the recorded result.
        run_path = os.path.join(home2, "runs", "demo-rp.json")
        with open(run_path) as fh:
            run = json.load(fh)
        run["probes"][0]["passed"] = False
        with open(run_path, "w") as fh:
            json.dump(run, fh)
        result = self.t12("red", "--home", home2, "--task-id", "demo-rp",
                          "--command", "python3 -m unittest tests.test_greet -q",
                          "--marker", "nope", "--test-path", "tests/test_greet.py",
                          "--session", "sess-test-writer", expect=1)
        self.assertIn("safety probes have not passed", result.stderr)

    def test_verify_passes_on_happy_path(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.t12("manifest", "--home", self.home, "--task-id", "demo-1")
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1")
        for name in ("event-chain", "evidence-refs", "exit-codes", "worktree", "manifest"):
            self.assertIn(f"PASS {name}", result.stdout)

    def test_verify_fails_on_tampered_run_record(self):
        self.do_red()
        run_path = os.path.join(self.home, "runs", "demo-1.json")
        with open(run_path) as fh:
            run = json.load(fh)
        run["title"] = "tampered title"
        with open(run_path, "w") as fh:
            json.dump(run, fh)
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("FAIL event-chain", result.stdout)
        self.assertIn("state digest", result.stdout)

    def test_verify_fails_on_tampered_event_log(self):
        self.do_red()
        events_path = os.path.join(self.home, "runs", "demo-1.events.jsonl")
        with open(events_path) as fh:
            lines = fh.readlines()
        event = json.loads(lines[0])
        event["detail"] = {"tampered": True}
        lines[0] = json.dumps(event, sort_keys=True) + "\n"
        with open(events_path, "w") as fh:
            fh.writelines(lines)
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("FAIL event-chain", result.stdout)

    def test_green_refuses_tampered_run_record(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        run_path = os.path.join(self.home, "runs", "demo-1.json")
        with open(run_path) as fh:
            run = json.load(fh)
        run["title"] = "tampered title"
        with open(run_path, "w") as fh:
            json.dump(run, fh)
        result = self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("event log failed verification", result.stderr)
        self.assertEqual(self.phase(), "green")

    def test_green_refuses_forged_gate(self):
        # A gate that fails, then a forged "passed" status in the record.
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(task_id="demo-fg", gates=[
                {"name": "unit", "command": ["python3", "-c", "import sys; sys.exit(1)"]},
            ]), fh)
        home2 = os.path.join(self.tmp.name, "home-fg")
        self.t12("init", "--home", home2, "--repo", self.repo)
        self.t12("prepare", "--home", home2, "--task", self.task_path)
        wt = os.path.join(home2, "worktrees", "demo-fg")
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home2, "--task-id", "demo-fg",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", home2, "--task-id", "demo-fg", "--session", "sess-builder")
        self.t12("gate", "--home", home2, "--task-id", "demo-fg", "--name", "unit", "--session", "sess-qa", expect=1)
        # Forge the pass in the record, then absorb the edit into the
        # chain with a probe run, so only the exit-code check can catch it.
        run_path = os.path.join(home2, "runs", "demo-fg.json")
        with open(run_path) as fh:
            run = json.load(fh)
        run["gates"][0]["status"] = "passed"
        with open(run_path, "w") as fh:
            json.dump(run, fh)
        self.t12("probe", "--home", home2, "--task-id", "demo-fg")
        self.t12("replay", "--home", home2, "--task-id", "demo-fg")
        result = self.t12("green", "--home", home2, "--task-id", "demo-fg", expect=1)
        self.assertIn("observation failed or timed out", result.stderr)
        out = self.t12("status", "--home", home2, "--task-id", "demo-fg").stdout
        self.assertEqual(json.loads(out)["phase"], "green")

    def test_manifest_tamper_detected(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.t12("manifest", "--home", self.home, "--task-id", "demo-1")
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1")
        self.assertIn("PASS manifest", result.stdout)
        manifest = os.path.join(self.home, "evidence", "demo-1", "manifest.txt")
        with open(manifest, "a") as fh:
            fh.write("tampered\n")
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("FAIL manifest", result.stdout)

    def run_to_verified(self, task_id):
        task_path = os.path.join(self.tmp.name, f"task-{task_id}.json")
        with open(task_path, "w") as fh:
            json.dump(task_dict(task_id=task_id), fh)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        wt = os.path.join(self.home, "worktrees", task_id)
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", self.home, "--task-id", task_id,
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", self.home, "--task-id", task_id, "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", task_id, "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", task_id)
        self.do_review(task_id=task_id)
        self.t12("green", "--home", self.home, "--task-id", task_id)

    def test_measurements_recorded_at_green(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            run = json.load(fh)
        m = run["measurements"]
        self.assertIsNotNone(m)
        self.assertGreater(m["wall_used_s"], 0)
        self.assertEqual(m["ops_used"], 6)
        self.assertEqual(m["repairs_used"], 0)
        self.assertEqual(m["gates"], 1)
        self.assertEqual(run["budget_source"], "task")

    def test_calibrate_refuses_with_too_few_runs(self):
        result = self.t12("calibrate", "--home", self.home, expect=1)
        self.assertIn("need at least 3", result.stdout)
        result = self.t12("report", "--home", self.home)
        self.assertIn("not enough data", result.stdout)

    def test_report_and_calibrate_end_to_end(self):
        for task_id in ("r1", "r2", "r3"):
            self.run_to_verified(task_id)
        result = self.t12("report", "--home", self.home)
        self.assertIn("3 verified", result.stdout)
        self.assertIn("calibration: from 3 completed runs", result.stdout)
        result = self.t12("calibrate", "--home", self.home)
        self.assertIn("recommended defaults", result.stdout)
        self.assertIn("dry run", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, "limits.json")))
        result = self.t12("calibrate", "--home", self.home, "--apply")
        self.assertIn("wrote", result.stdout)
        with open(os.path.join(self.home, "limits.json")) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["time_limit_s"], 600)
        self.assertEqual(saved["op_limit"], 20)
        self.assertEqual(saved["repair_cap"], 2)
        self.assertEqual(saved["calibrated_from"]["runs"], 3)
        # A task without budgets now inherits the calibrated defaults.
        task = task_dict(task_id="r4")
        for key in ("time_limit_s", "op_limit", "repair_cap"):
            del task[key]
        task_path = os.path.join(self.tmp.name, "task-r4.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        with open(os.path.join(self.home, "runs", "r4.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["budgets"]["time_limit_s"], 600)
        self.assertEqual(run["budgets"]["op_limit"], 20)
        self.assertEqual(run["budgets"]["repair_cap"], 2)
        self.assertEqual(run["budget_source"], "calibrated")
        # A task with explicit budgets keeps them.
        task = task_dict(task_id="r5", gates=None)
        task["time_limit_s"] = 123
        task_path = os.path.join(self.tmp.name, "task-r5.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        with open(os.path.join(self.home, "runs", "r5.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["budgets"]["time_limit_s"], 123)
        self.assertEqual(run["budget_source"], "task")

    def test_session_separation_enforced(self):
        self.do_red()
        result = self.t12("stage", "--home", self.home, "--task-id", "demo-1",
                          "--session", "sess-test-writer", expect=1)
        self.assertIn("already bound to the test-writer role", result.stderr)
        self.do_implement_and_stage()
        result = self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                          "--name", "unit", "--session", "sess-builder", expect=1)
        self.assertIn("already bound to the builder role", result.stderr)
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        report = os.path.join(self.tmp.name, "review-x.md")
        with open(report, "w") as fh:
            fh.write("Verdict: approve\n")
        for bad in ("sess-test-writer", "sess-builder", "sess-qa"):
            result = self.t12("review", "--home", self.home, "--task-id", "demo-1",
                              "--role", "reviewer", "--session", bad,
                              "--verdict", "approve", "--report", report, expect=1)
            self.assertIn("already bound", result.stderr)
        self.do_review()

    def test_green_requires_review(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        result = self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("requires an approving reviewer review", result.stderr)
        self.do_review()
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")

    def test_changes_requested_blocks_green(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review(verdict="changes-requested")
        result = self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("blocked", result.stderr)
        self.do_review()
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")

    def test_restage_voids_review(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        # Re-stage a new candidate: the review is voided, green refuses.
        self.write_worktree("app.py", APP_V2 + "# note: re-staged\n")
        self.commit_worktree("feat: note")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        result = self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("requires an approving reviewer review", result.stderr)
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            run = json.load(fh)
        self.assertTrue(run["reviews"][0]["voided"])
        self.do_review()
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["reviews"][1]["prior_review"],
                         run["reviews"][0]["report"]["name"])
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")

    def test_security_review_required(self):
        task = task_dict(task_id="demo-sec")
        task["required_reviews"] = ["reviewer", "security"]
        task_path = os.path.join(self.tmp.name, "task-demo-sec.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        wt = os.path.join(self.home, "worktrees", "demo-sec")
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", self.home, "--task-id", "demo-sec",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-sec",
                 "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", "demo-sec",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-sec")
        self.do_review(task_id="demo-sec")
        result = self.t12("green", "--home", self.home, "--task-id", "demo-sec", expect=1)
        self.assertIn("requires an approving security review", result.stderr)
        self.do_review(task_id="demo-sec", role="security", session="sess-security")
        self.t12("green", "--home", self.home, "--task-id", "demo-sec")
        out = self.t12("status", "--home", self.home, "--task-id", "demo-sec").stdout
        self.assertEqual(json.loads(out)["phase"], "local-verified")

    def test_review_report_stored_and_verified(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        report = os.path.join(self.home, "evidence", "demo-1", "review-reviewer-1.md")
        self.assertTrue(os.path.exists(report))
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(result.returncode, 0)
        with open(report, "a") as fh:
            fh.write("forged\n")
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("FAIL evidence-refs", result.stdout + result.stderr)

    def test_context_shows_roles_and_prior_review(self):
        self.do_red()
        self.do_implement_and_stage()
        result = self.t12("context", "--home", self.home, "--task-id", "demo-1",
                          "--role", "reviewer", "--phase", "gates")
        context = json.loads(result.stdout.split("--- context (JSON) ---\n", 1)[1])
        self.assertEqual(context["roles"]["test-writer"]["session"], "sess-test-writer")
        self.assertEqual(context["roles"]["builder"]["session"], "sess-builder")
        self.assertIsNone(context["prior_review"])
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        self.write_worktree("app.py", APP_V2 + "# note: re-staged\n")
        self.commit_worktree("feat: note")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        result = self.t12("context", "--home", self.home, "--task-id", "demo-1",
                          "--role", "reviewer", "--phase", "gates")
        context = json.loads(result.stdout.split("--- context (JSON) ---\n", 1)[1])
        self.assertTrue(context["prior_review"]["voided"])
        self.assertEqual(context["prior_review"]["report"], "review-reviewer-1.md")

    def test_gate_failure_counts_repair_and_restage_resets(self):
        self.do_red()
        self.write_worktree("app.py", APP_WRONG)  # close, but still failing
        self.commit_worktree("feat: wrong implementation")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        result = self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                          "--name", "unit", "--session", "sess-qa", expect=1)
        self.assertIn("failed", result.stderr)
        status = json.loads(self.t12(
            "status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(status["budgets"]["repairs_used"], 1)
        # Fix, re-stage: gate observations reset, candidate re-pinned.
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: fix greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")

    def stage_without_implementation(self):
        self.do_red()
        self.write_worktree("NOTES.md", "# notes\n")
        self.commit_worktree("chore: no implementation")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")

    def test_gate_refuses_uncommitted_changes(self):
        self.stage_without_implementation()
        self.write_worktree("app.py", APP_V2)
        result = self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                          "--name", "unit", "--session", "sess-qa", expect=1)
        self.assertIn("uncommitted changes", result.stderr)

    def test_gate_refuses_moved_head(self):
        self.stage_without_implementation()
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: implement after stage")
        result = self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                          "--name", "unit", "--session", "sess-qa", expect=1)
        self.assertIn("HEAD moved since stage", result.stderr)

    def test_replay_refuses_uncommitted_changes(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.write_worktree("app.py", APP_WRONG)
        result = self.t12("replay", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("uncommitted changes", result.stderr)

    def test_gate_ignores_files_outside_the_candidate(self):
        self.do_red()
        self.write_worktree(".gitignore", "app_fix.py\n")
        self.write_worktree("app.py", "from app_fix import greet\n")
        self.commit_worktree("feat: greet from an ignored module")
        self.write_worktree("app_fix.py", APP_V2)
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        result = self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                          "--name", "unit", "--session", "sess-qa", expect=1)
        self.assertIn("gate 'unit' failed", result.stderr)

    def test_green_refuses_gate_bound_to_another_commit(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        run_path = os.path.join(self.home, "runs", "demo-1.json")
        with open(run_path) as fh:
            run = json.load(fh)
        run["gates"][0]["commit"] = "0" * 40
        with open(run_path, "w") as fh:
            json.dump(run, fh)
        self.t12("probe", "--home", self.home, "--task-id", "demo-1")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.do_review()
        result = self.t12("green", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("not run on the candidate", result.stderr)

    def test_red_ignores_files_outside_the_commit(self):
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: greet already works")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as fh:
            fh.write("app/\n")
        self.write_worktree("app/__init__.py", APP_V1)
        self.write_worktree("tests/test_greet.py", TEST_FILE)
        result = self.t12(
            "red", "--home", self.home, "--task-id", "demo-1",
            "--test-path", "tests/test_greet.py",
            "--command", "python3 -m unittest tests.test_greet -q",
            "--marker", "FAILED", "--session", "sess-test-writer", expect=1,
        )
        self.assertIn("did not fail", result.stderr)

    def test_red_commit_is_the_commit_it_ran_on(self):
        self.do_red()
        status = json.loads(self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(self._git(self.worktree, "rev-parse", "HEAD"), self.run_record()["red"]["commit"])
        self.assertEqual(self._git(self.worktree, "status", "--porcelain"), "")
        self.assertEqual(status["phase"], "red")

    def run_record(self, home=None, task_id="demo-1"):
        with open(os.path.join(home or self.home, "runs", f"{task_id}.json")) as fh:
            return json.load(fh)

    def test_verify_flags_gate_not_run_on_candidate(self):
        self.do_red()
        self.do_implement_and_stage()
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        run_path = os.path.join(self.home, "runs", "demo-1.json")
        run = self.run_record()
        run["gates"][0]["commit"] = "0" * 40
        with open(run_path, "w") as fh:
            json.dump(run, fh)
        self.t12("probe", "--home", self.home, "--task-id", "demo-1")
        result = self.t12("verify", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("FAIL exit-codes", result.stdout)
        self.assertIn("not run on the candidate", result.stdout)

    def prepare_custom(self, task_id, **fields):
        task = task_dict(task_id=task_id)
        task.update(fields)
        path = os.path.join(self.tmp.name, f"task-{task_id}.json")
        with open(path, "w") as fh:
            json.dump(task, fh)
        home = os.path.join(self.tmp.name, f"home-{task_id}")
        self.t12("init", "--home", home, "--repo", self.repo)
        return home, path

    def test_verify_deps_reach_gates_read_only(self):
        home, task = self.prepare_custom("demo-deps", verify_deps=["vendor"])
        self.t12("prepare", "--home", home, "--task", task)
        wt = os.path.join(home, "worktrees", "demo-deps")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as fh:
            fh.write("vendor/\n")
        os.makedirs(os.path.join(wt, "vendor"))
        with open(os.path.join(wt, "vendor", "helper.py"), "w") as fh:
            fh.write(APP_V2)
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home, "--task-id", "demo-deps",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write("import os, sys\n"
                     "sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'vendor'))\n"
                     "from helper import greet\n")
        self._git(wt, "add", "app.py")
        self._git(wt, "commit", "-m", "feat: greet from vendored helper")
        self.t12("stage", "--home", home, "--task-id", "demo-deps", "--session", "sess-builder")
        self.t12("gate", "--home", home, "--task-id", "demo-deps", "--name", "unit", "--session", "sess-qa")
        gate = self.run_record(home, "demo-deps")["gates"][0]
        self.assertEqual(gate["observation"]["observation"]["deps"], ["vendor"])
        self.assertTrue(os.path.exists(os.path.join(wt, "vendor", "helper.py")),
                        "cleanup must not delete the builder's dependency folder")

    def test_verify_deps_must_stay_inside_the_worktree(self):
        for bad in (["../outside"], ["/abs"], [".git"], "vendor"):
            home, task = self.prepare_custom("demo-bad", verify_deps=bad)
            result = self.t12("prepare", "--home", home, "--task", task, expect=1)
            self.assertIn("verify_deps", result.stderr)
            shutil.rmtree(home)

    def act(self, role, session, output, expect=0):
        path = os.path.join(self.tmp.name, f"act-{role}-{session}.json")
        with open(path, "w") as fh:
            fh.write(output if isinstance(output, str) else json.dumps(output))
        return self.t12("act", "--home", self.home, "--task-id", "demo-1",
                        "--role", role, "--session", session, "--input", path, expect=expect)

    def test_act_drives_a_run_from_worker_outputs(self):
        self.write_worktree("tests/test_greet.py", TEST_FILE)
        self.act("test-writer", "sess-tw", {"tool": "red_test_ready", "args": {
            "test_path": "tests/test_greet.py",
            "command": ["python3", "-m", "unittest", "tests.test_greet", "-q"],
            "marker": "FAILED"}})
        self.assertEqual(self.phase(), "red")
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: implement greet")
        self.act("builder", "sess-b", {"tool": "implementation_ready", "args": {"summary": "greet formats the name"}})
        self.assertEqual(self.phase(), "gates")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.act("qa", "sess-qa", {"tool": "qa_report", "args": {
            "verdict": "pass", "commands": ["python3 -m unittest"], "failures": []}})
        self.act("reviewer", "sess-rv", {"tool": "review", "args": {
            "verdict": "merge-ready", "findings": [], "verification": "unit gate re-run: OK"}})
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.phase(), "local-verified")
        events = self.event_types()
        self.assertEqual(events.count("worker-output"), 4)

    def event_types(self):
        with open(os.path.join(self.home, "runs", "demo-1.events.jsonl")) as fh:
            return [json.loads(line)["type"] for line in fh if line.strip()]

    def test_act_rejects_malformed_output_as_a_failed_step(self):
        cases = [
            ("not json", "not valid JSON"),
            ({"tool": "merge", "args": {}}, "unknown tool"),
            ({"tool": "implementation_ready", "args": {"summary": "x"}}, "not allowed for role"),
            ({"tool": "red_test_ready", "args": {"test_path": "t.py"}}, "missing"),
            ({"tool": "red_test_ready", "args": {"test_path": "t.py", "command": "python3 t.py",
                                                 "marker": "F"}}, "command"),
            ({"tool": "escalate", "args": {"question": "q", "extra": 1}}, "unexpected"),
        ]
        for output, message in cases:
            result = self.act("test-writer", "sess-tw", output, expect=1)
            self.assertIn(message, result.stderr)
        self.assertEqual(self.phase(), "ready")
        self.assertEqual(self.event_types().count("worker-output-rejected"), len(cases))
        status = json.loads(self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout)
        self.assertEqual(status["budgets"]["ops_used"], len(cases))

    def test_escalation_waits_for_and_records_the_human_answer(self):
        self.act("test-writer", "sess-tw", {"tool": "escalate", "args": {
            "question": "Should greet() strip whitespace?"}})
        self.assertEqual(self.phase(), "paused")
        result = self.t12("resume", "--home", self.home, "--task-id", "demo-1", expect=1)
        self.assertIn("Should greet() strip whitespace?", result.stderr)
        self.t12("resume", "--home", self.home, "--task-id", "demo-1", "--answer", "No, keep it as given.")
        self.assertEqual(self.phase(), "ready")
        self.assertIn("human-response", self.event_types())
        out = self.t12("context", "--home", self.home, "--task-id", "demo-1",
                       "--role", "test-writer", "--phase", "red").stdout
        context = json.loads(out.split("--- context (JSON) ---", 1)[1])
        self.assertEqual(context["human_responses"], [
            {"question": "Should greet() strip whitespace?", "answer": "No, keep it as given."}])
        self.assertIn("red_test_ready", [t["name"] for t in context["tools"]])

    def test_red_records_the_committed_test_not_a_later_edit(self):
        self.write_worktree("tests/test_greet.py", TEST_FILE)
        test_file = os.path.join(self.worktree, "tests", "test_greet.py")
        proc = subprocess.Popen(
            [BIN, "red", "--home", self.home, "--task-id", "demo-1",
             "--test-path", "tests/test_greet.py",
             "--command", "python3 -c \"import time; time.sleep(2); print('FAILED'); raise SystemExit(1)\"",
             "--marker", "FAILED", "--session", "sess-test-writer"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        time.sleep(1)
        with open(test_file, "a") as fh:
            fh.write("# edited while red ran\n")
        self.assertEqual(proc.wait(timeout=60), 0, proc.stderr.read())
        red = self.run_record()["red"]
        committed = self._git(self.worktree, "rev-parse", f"{red['commit']}:tests/test_greet.py")
        self.assertEqual(red["test_blob"], committed)

    def test_red_accepts_an_edit_to_an_existing_test_file(self):
        self.write_worktree("tests/test_greet.py", "import unittest\n")
        self.commit_worktree("test: placeholder")
        self.do_red()

    def test_red_accepts_a_test_in_a_new_directory(self):
        self.write_worktree("tests/unit/test_greet.py", TEST_FILE)
        self.t12("red", "--home", self.home, "--task-id", "demo-1",
                 "--test-path", "tests/unit/test_greet.py",
                 "--command", "python3 -m unittest discover -s tests/unit -t tests/unit -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        self.assertEqual(self.phase(), "red")

    def test_verify_deps_may_not_overlap(self):
        for bad in (["vendor", "vendor/sub"], ["vendor", "vendor"], ["vendor/sub", "vendor"]):
            home, task = self.prepare_custom("demo-overlap", verify_deps=bad)
            result = self.t12("prepare", "--home", home, "--task", task, expect=1)
            self.assertIn("overlap", result.stderr)
            shutil.rmtree(home)

    def test_act_stops_at_the_operation_budget(self):
        home, task = self.prepare_custom("demo-ops", op_limit=2)
        self.t12("prepare", "--home", home, "--task", task)
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w") as fh:
            fh.write("not json")
        for _ in range(2):
            self.t12("act", "--home", home, "--task-id", "demo-ops", "--role", "builder",
                     "--session", "s", "--input", path, expect=1)
        result = self.t12("act", "--home", home, "--task-id", "demo-ops", "--role", "builder",
                          "--session", "s", "--input", path, expect=1)
        self.assertIn("operation budget exhausted", result.stderr)
        self.assertEqual(self.run_record(home, "demo-ops")["budgets"]["ops_used"], 2)

    def test_act_refuses_while_paused_and_binds_the_qa_session(self):
        qa = {"tool": "qa_report", "args": {"verdict": "pass", "commands": [], "failures": []}}
        self.act("qa", "sess-qa", qa)
        result = self.act("builder", "sess-qa", {"tool": "implementation_ready",
                                                 "args": {"summary": "x"}}, expect=1)
        self.assertIn("already bound to the qa role", result.stderr)
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "lunch")
        result = self.act("qa", "sess-qa", qa, expect=1)
        self.assertIn("paused", result.stderr)

    def test_blank_answer_is_refused(self):
        self.t12("escalate", "--home", self.home, "--task-id", "demo-1", "--question", "Which locale?")
        result = self.t12("resume", "--home", self.home, "--task-id", "demo-1",
                          "--answer", "   ", expect=1)
        self.assertIn("Which locale?", result.stderr)
        self.assertEqual(self.phase(), "paused")

    def test_failed_red_costs_an_operation(self):
        self.write_worktree("app.py", APP_V2)
        self.commit_worktree("feat: greet already works")
        self.write_worktree("tests/test_greet.py", TEST_FILE)
        self.t12("red", "--home", self.home, "--task-id", "demo-1",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer", expect=1)
        self.assertEqual(self.run_record()["budgets"]["ops_used"], 1)

    def test_answer_without_an_open_question_is_refused(self):
        self.t12("pause", "--home", self.home, "--task-id", "demo-1", "--reason", "lunch")
        result = self.t12("resume", "--home", self.home, "--task-id", "demo-1",
                          "--answer", "unprompted", expect=1)
        self.assertIn("no open question", result.stderr)

    def spawn(self, *args):
        return subprocess.Popen([BIN, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def test_concurrent_commands_on_one_run_keep_the_log_intact(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w") as fh:
            fh.write("not json")
        procs = [self.spawn("act", "--home", self.home, "--task-id", "demo-1", "--role", "builder",
                            "--session", "s", "--input", path) for _ in range(10)]
        errors = [p.communicate(timeout=60)[1] for p in procs]
        rejected = [e for e in errors if "not valid JSON" in e]
        busy = [e for e in errors if "is busy" in e]
        self.assertEqual(len(rejected) + len(busy), len(procs), errors)
        self.t12("verify", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.event_types().count("worker-output-rejected"), len(rejected))
        self.assertEqual(self.run_record()["budgets"]["ops_used"], len(rejected))

    def drive_to_verified(self, home, task_id, app=APP_V2):
        wt = os.path.join(home, "worktrees", task_id)
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home, "--task-id", task_id,
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(app)
        self._git(wt, "commit", "-qam", "feat: implement greet")
        self.t12("stage", "--home", home, "--task-id", task_id, "--session", "sess-builder")
        self.t12("gate", "--home", home, "--task-id", task_id, "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", home, "--task-id", task_id)
        self.do_review(home=home, task_id=task_id)
        self.t12("green", "--home", home, "--task-id", task_id)
        return self.run_record(home, task_id)["candidate"]["commit"]

    def test_red_refuses_a_test_outside_touches(self):
        home, task = self.prepare_custom("demo-red-narrow", touches=["app.py"])
        self.t12("prepare", "--home", home, "--task", task)
        with open(os.path.join(home, "worktrees", "demo-red-narrow", "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        result = self.t12("red", "--home", home, "--task-id", "demo-red-narrow",
                          "--test-path", "tests/test_greet.py",
                          "--command", "python3 -m unittest tests.test_greet -q",
                          "--marker", "FAILED", "--session", "sess-test-writer", expect=1)
        self.assertIn("outside the task's touches: tests/test_greet.py", result.stderr)

    def test_stage_refuses_changes_outside_touches(self):
        home, task = self.prepare_custom("demo-narrow", touches=["tests"])
        self.t12("prepare", "--home", home, "--task", task)
        wt = os.path.join(home, "worktrees", "demo-narrow")
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home, "--task-id", "demo-narrow",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "commit", "-qam", "feat: implement greet")
        result = self.t12("stage", "--home", home, "--task-id", "demo-narrow",
                          "--session", "sess-builder", expect=1)
        self.assertIn("outside the task's touches: app.py", result.stderr)

        home, task = self.prepare_custom("demo-wide", touches=["app.py", "tests"])
        self.t12("prepare", "--home", home, "--task", task)
        self.drive_to_verified(home, "demo-wide")

    def test_touches_and_depends_on_are_checked_at_admission(self):
        for field, bad, message in (("touches", ["../x"], "touches"),
                                    ("touches", "app.py", "touches"),
                                    ("depends_on", ["no spaces"], "depends_on")):
            home, task = self.prepare_custom("demo-bad", **{field: bad})
            result = self.t12("prepare", "--home", home, "--task", task, expect=1)
            self.assertIn(message, result.stderr)
            shutil.rmtree(home)

    def queue(self, home, queue_dir, max_runs):
        result = subprocess.run(
            [os.path.join(ROOT, "bin", "t12-queue"), "--home", home, "--queue", queue_dir,
             "--max", str(max_runs)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = {}
        for line in result.stdout.splitlines():
            verdict, rest = line.split(" ", 1)
            task_id, _, why = rest.partition(": ")
            lines[task_id] = (verdict, why)
        return lines

    def test_queue_starts_only_tasks_that_cannot_collide(self):
        home = os.path.join(self.tmp.name, "home-queue")
        self.t12("init", "--home", home, "--repo", self.repo)
        queue_dir = os.path.join(self.tmp.name, "queue")
        os.makedirs(queue_dir)
        for name, fields in (("a", {"touches": ["app.py", "tests"]}),
                             ("b", {"touches": ["app.py"]}),
                             ("c", {"touches": ["docs"]}),
                             ("d", {"touches": ["lib"], "depends_on": ["q-a"]}),
                             ("e", {"touches": ["./app.py"]})):
            task = task_dict(task_id=f"q-{name}")
            task.update(fields)
            with open(os.path.join(queue_dir, f"{name}.json"), "w") as fh:
                json.dump(task, fh)

        self.assertEqual(self.queue(home, queue_dir, 2), {
            "q-a": ("started", ""),
            "q-b": ("waiting", "touches overlap q-a"),
            "q-c": ("started", ""),
            "q-d": ("waiting", "depends on q-a, not merged yet"),
            "q-e": ("waiting", "touches overlap q-a"),
        })
        self.assertEqual(self.queue(home, queue_dir, 2), {
            "q-b": ("waiting", "touches overlap q-a"),
            "q-d": ("waiting", "depends on q-a, not merged yet"),
            "q-e": ("waiting", "touches overlap q-a"),
        })

        candidate = self.drive_to_verified(home, "q-a")
        self.assertEqual(self.queue(home, queue_dir, 2)["q-b"],
                         ("waiting", "touches overlap q-a"))
        self._git(self.repo, "merge", "--ff-only", candidate)
        self.assertEqual(self.queue(home, queue_dir, 2), {
            "q-b": ("started", ""),
            "q-d": ("waiting", "at capacity (2 active)"),
            "q-e": ("waiting", "touches overlap q-b"),
        })
        self.t12("stop", "--home", home, "--task-id", "q-c", "--reason", "done elsewhere")
        self.assertEqual(self.queue(home, queue_dir, 2), {
            "q-d": ("started", ""),
            "q-e": ("waiting", "touches overlap q-b"),
        })
        self.assertEqual(self.run_record(home, "q-d")["base_commit"], candidate)

    def act_in(self, home, task_id, role, session, output, expect=0):
        path = os.path.join(self.tmp.name, f"act-{task_id}-{role}.json")
        with open(path, "w") as fh:
            json.dump(output, fh)
        return self.t12("act", "--home", home, "--task-id", task_id, "--role", role,
                        "--session", session, "--input", path, expect=expect)

    def run_with_recurring_mistakes(self, home, task_id):
        self.act_in(home, task_id, "reviewer", "sess-reviewer", {"tool": "review", "args": {
            "verdict": "blocked", "verification": "too early",
            "findings": [{"id": "R0", "class": ["Bug"], "severity": "Blocker"}]}}, expect=1)
        self.act_in(home, task_id, "test-writer", "sess-tw", {"tool": "red_test_ready", "args": {
            "test_path": "tests/test_greet.py", "command": ["python3", "t.py"]}}, expect=1)
        self.t12("escalate", "--home", home, "--task-id", task_id, "--question", "Which locale?")
        self.t12("resume", "--home", home, "--task-id", task_id, "--answer", "en only")
        wt = os.path.join(home, "worktrees", task_id)
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home, "--task-id", task_id,
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-tw")
        for app, expect in ((APP_WRONG, 1), (APP_V2, 0)):
            with open(os.path.join(wt, "app.py"), "w") as fh:
                fh.write(app)
            self._git(wt, "commit", "-qam", "feat: implement greet")
            self.t12("stage", "--home", home, "--task-id", task_id, "--session", "sess-builder")
            self.t12("gate", "--home", home, "--task-id", task_id, "--name", "unit",
                     "--session", "sess-qa", expect=expect)
        self.t12("replay", "--home", home, "--task-id", task_id)
        self.act_in(home, task_id, "reviewer", "sess-reviewer", {"tool": "review", "args": {
            "verdict": "blocked", "verification": "re-ran the gate",
            "findings": [{"id": "R1", "class": "Verification miss", "severity": "Blocker",
                          "where": "tests/test_greet.py:1", "finding": "no edge case",
                          "owner": "qa"}]}})

    def test_retro_proposes_a_check_for_each_recurring_lesson(self):
        home = os.path.join(self.tmp.name, "home-retro")
        self.t12("init", "--home", home, "--repo", self.repo)
        for task_id in ("lesson-1", "lesson-2"):
            path = os.path.join(self.tmp.name, f"{task_id}.json")
            with open(path, "w") as fh:
                json.dump(task_dict(task_id=task_id), fh)
            self.t12("prepare", "--home", home, "--task", path)
            self.run_with_recurring_mistakes(home, task_id)

        def snapshot():
            files = {}
            for root, _, names in os.walk(home):
                for name in names:
                    with open(os.path.join(root, name), "rb") as fh:
                        files[os.path.join(root, name)] = fh.read()
            return files

        before = snapshot()
        report = json.loads(self.t12("retro", "--home", home, "--json", "--min", "2").stdout)
        self.assertEqual(snapshot(), before)
        self.assertEqual(report["gates"]["unit"]["failed_runs"], 2)
        self.assertEqual(len(report["gates"]["unit"]["durations_s"]), 4)
        kinds = {p["kind"]: p for p in report["proposals"]}
        self.assertEqual(set(kinds), {"gate", "rejected-output", "finding", "escalation"})
        self.assertIn("unit failed in 2 of 2 runs", kinds["gate"]["evidence"])
        self.assertIn("Verification miss in 2 of 2", kinds["finding"]["evidence"])
        self.assertEqual(report["runs_with_finding"], {"Verification miss": 2})
        self.assertIn("Which locale?", kinds["escalation"]["evidence"])
        self.assertIn("missing", kinds["rejected-output"]["evidence"])
        text = self.t12("retro", "--home", home, "--min", "2").stdout
        self.assertIn("Verification miss", text)
        self.assertIn("changes nothing", text)

    def test_a_factory_inside_the_state_home_refuses_to_run(self):
        copy = os.path.join(self.home, "worktrees", "demo-1")
        shutil.copytree(os.path.join(ROOT, "t12"), os.path.join(copy, "t12"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "bin"), os.path.join(copy, "bin"))
        result = subprocess.run(
            [os.path.join(copy, "bin", "t12"), "status", "--home", self.home, "--task-id", "demo-1"],
            capture_output=True, text=True, timeout=60, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("runs from inside the state home", result.stderr)

    def test_policy_paths_on_the_base_require_a_security_review(self):
        os.makedirs(os.path.join(self.repo, ".t12"))
        with open(os.path.join(self.repo, ".t12", "policy.json"), "w") as fh:
            json.dump({"security_review_paths": ["app.py"]}, fh)
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-qm", "policy")
        home, task = self.prepare_custom("demo-policy")
        self.t12("prepare", "--home", home, "--task", task)
        wt = os.path.join(home, "worktrees", "demo-policy")
        with open(os.path.join(wt, ".t12", "policy.json"), "w") as fh:
            json.dump({"security_review_paths": []}, fh)
        self._git(wt, "commit", "-qam", "chore: try to drop the policy")
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.t12("red", "--home", home, "--task-id", "demo-policy",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "commit", "-qam", "feat: implement greet")
        self.t12("stage", "--home", home, "--task-id", "demo-policy", "--session", "sess-builder")
        self.assertEqual(self.run_record(home, "demo-policy")["required_reviews"],
                         ["reviewer", "security"])
        self.t12("gate", "--home", home, "--task-id", "demo-policy", "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", home, "--task-id", "demo-policy")
        self.do_review(home=home, task_id="demo-policy")
        result = self.t12("green", "--home", home, "--task-id", "demo-policy", expect=1)
        self.assertIn("security review", result.stderr)
        self.do_review(home=home, task_id="demo-policy", role="security", session="sess-security")
        self.t12("green", "--home", home, "--task-id", "demo-policy")

    def board(self):
        rows = json.loads(self.t12("board", "--home", self.home, "--json").stdout)
        return {row["task_id"]: row for row in rows}

    def test_board_shows_every_run_and_what_it_waits_on(self):
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(task_id="demo-2"), fh)
        self.t12("prepare", "--home", self.home, "--task", self.task_path)
        self.do_red()
        self.t12("escalate", "--home", self.home, "--task-id", "demo-2", "--question", "Which locale?")
        rows = self.board()
        self.assertEqual(rows["demo-1"]["phase"], "red")
        self.assertEqual(rows["demo-1"]["next"], "builder: stage")
        self.assertEqual(rows["demo-1"]["ops_left"], 49)
        self.assertEqual(rows["demo-2"]["next"], "human: answer 'Which locale?'")
        self.assertFalse(rows["demo-1"]["busy"])

        self.do_implement_and_stage()
        self.assertEqual(self.board()["demo-1"]["next"], "qa: gate unit")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1", "--name", "unit", "--session", "sess-qa")
        self.assertEqual(self.board()["demo-1"]["next"], "replay")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        self.assertEqual(self.board()["demo-1"]["next"], "reviewer: review")
        self.do_review()
        self.assertEqual(self.board()["demo-1"]["next"], "green")

        lock = os.open(os.path.join(self.home, "runs", "demo-1.lock"), os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertTrue(self.board()["demo-1"]["busy"])
            result = self.t12("stop", "--home", self.home, "--task-id", "demo-1",
                              "--reason", "x", expect=1)
            self.assertIn("is busy", result.stderr)
        finally:
            os.close(lock)
        text = self.t12("board", "--home", self.home).stdout
        self.assertIn("demo-2", text)
        self.assertIn("human: answer", text)

    def test_two_tasks_run_at_the_same_time(self):
        task = task_dict(task_id="demo-2")
        path = os.path.join(self.tmp.name, "task-demo-2.json")
        with open(path, "w") as fh:
            json.dump(task, fh)
        self.t12("prepare", "--home", self.home, "--task", path)
        ids = ("demo-1", "demo-2")
        for task_id in ids:
            wt = os.path.join(self.home, "worktrees", task_id)
            with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
                fh.write(TEST_FILE)
            self.t12("red", "--home", self.home, "--task-id", task_id,
                     "--test-path", "tests/test_greet.py",
                     "--command", "python3 -m unittest tests.test_greet -q",
                     "--marker", "FAILED", "--session", "sess-test-writer")
            with open(os.path.join(wt, "app.py"), "w") as fh:
                fh.write(APP_V2)
            self._git(wt, "commit", "-qam", "feat: implement greet")
            self.t12("stage", "--home", self.home, "--task-id", task_id, "--session", "sess-builder")
        for step in (("gate", "--name", "unit", "--session", "sess-qa"), ("replay",)):
            procs = [self.spawn(step[0], "--home", self.home, "--task-id", t, *step[1:]) for t in ids]
            for proc in procs:
                _, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err)
        for task_id in ids:
            self.assertEqual(self.run_record(task_id=task_id)["phase"], "green")
            self.t12("verify", "--home", self.home, "--task-id", task_id)


class DockerFlow(unittest.TestCase):
    """A product repo and the commands that drive a docker-adapter run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.repo, "tests"))
        self._git(self.repo, "init", "-b", "main", "-q")
        self._git(self.repo, "config", "user.email", "t@example.com")
        self._git(self.repo, "config", "user.name", "T")
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write(APP_V1)
        open(os.path.join(self.repo, "tests", "__init__.py"), "w").close()
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-qm", "init")
        self.docker_env = {"GITHUB_TOKEN": "canary-secret-value"}

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def dt12(self, *args, expect=0, extra_env=None):
        env = dict(self.docker_env)
        if extra_env:
            env.update(extra_env)
        return self.t12(*args, expect=expect, extra_env=env)

    def t12(self, *args, expect=0, extra_env=None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [BIN, *args], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, expect,
                         f"t12 {' '.join(args)} -> {result.returncode}: {result.stdout} {result.stderr}")
        return result

    def prepare_task(self, task_id, image=None, extra_env=None):
        task = task_dict(task_id=task_id)
        task["sandbox_adapter"] = "docker"
        if image:
            task["sandbox_image"] = image
        task_path = os.path.join(self.tmp.name, f"task-{task_id}.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        self.dt12("init", "--repo", self.repo, "--home", self.home)
        self.dt12("prepare", "--home", self.home, "--task", task_path,
                  extra_env=extra_env)
        return os.path.join(self.home, "worktrees", task_id)

    def run_to_verified(self, task_id):
        wt = os.path.join(self.home, "worktrees", task_id)
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)
        self.dt12("red", "--home", self.home, "--task-id", task_id,
                  "--test-path", "tests/test_greet.py",
                  "--command", "python3 -m unittest tests.test_greet -q",
                  "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.dt12("stage", "--home", self.home, "--task-id", task_id,
                  "--session", "sess-builder")
        self.dt12("gate", "--home", self.home, "--task-id", task_id,
                  "--name", "unit", "--session", "sess-qa")
        self.dt12("replay", "--home", self.home, "--task-id", task_id)
        report = os.path.join(self.tmp.name, f"review-{task_id}.md")
        with open(report, "w") as fh:
            fh.write("Verdict: approve\n")
        self.dt12("review", "--home", self.home, "--task-id", task_id,
                  "--role", "reviewer", "--session", "sess-reviewer",
                  "--verdict", "approve", "--report", report)
        self.dt12("green", "--home", self.home, "--task-id", task_id)
        self.dt12("manifest", "--home", self.home, "--task-id", task_id)


class DockerCase(DockerFlow):
    """Docker adapter end-to-end against the fake docker stub, for
    machines without Docker. The stub emulates the daemon and stands
    in for the container's confinement with a Landlock cage."""

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "docker")
        with open(os.path.join(os.path.dirname(__file__), "fake_docker.py"), "rb") as src:
            with open(stub, "wb") as dst:
                dst.write(src.read())
        os.chmod(stub, 0o755)
        self.log_path = os.path.join(self.tmp.name, "docker.log")
        self.docker_env.update({
            "PATH": bindir + ":" + os.environ["PATH"],
            "FAKE_DOCKER_ROOT": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "FAKE_DOCKER_LOG": self.log_path,
        })

    def docker_log(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_docker_end_to_end(self):
        if not sandbox.landlock_available():
            self.skipTest("fake docker stands in for confinement with Landlock")
        wt = self.prepare_task("demo-docker", image="python:3.12-slim")
        self.run_to_verified("demo-docker")
        with open(os.path.join(self.home, "runs", "demo-docker.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["sandbox_adapter"], "docker")
        self.assertEqual(run["sandbox_image"], "python:3.12-slim")
        self.assertTrue(sandbox.probes_passed(run["probes"]))
        with open(os.path.join(self.home, "evidence", "demo-docker", "probes.json")) as fh:
            self.assertEqual(json.load(fh)["adapter"], "docker")
        out = self.dt12("status", "--home", self.home, "--task-id", "demo-docker").stdout
        status = json.loads(out)
        self.assertEqual(status["phase"], "local-verified")
        self.assertEqual(status["sandbox_adapter"], "docker")
        self.assertEqual(status["sandbox_image"], "python:3.12-slim")
        with open(os.path.join(self.home, "evidence", "demo-docker", "manifest.txt")) as fh:
            self.assertIn("docker (image python:3.12-slim)", fh.read())
        log = self.docker_log()
        runs = [entry for entry in log if entry[:1] == ["run"]]
        self.assertTrue(runs, "no docker run invocations recorded")
        checkout = os.path.join(self.home, "verify", "demo-docker")
        for entry in runs:
            self.assertIn("--network", entry)
            self.assertEqual(entry[entry.index("--network") + 1], "none")
            self.assertTrue(f"{wt}:{wt}:rw" in entry or f"{checkout}:{checkout}:rw" in entry,
                            f"unexpected mounts: {entry}")
            self.assertIn("python:3.12-slim", entry)
        self.assertTrue(any(f"{checkout}:{checkout}:rw" in entry for entry in runs),
                        "gate and replay did not run in the candidate checkout")
        self.assertFalse(os.path.exists(checkout), "candidate checkout was not removed")
        flat = " ".join(" ".join(entry) for entry in log)
        self.assertNotIn("canary-secret-value", flat)

    def test_docker_image_defaults(self):
        wt = self.prepare_task("demo-default")
        with open(os.path.join(self.home, "runs", "demo-default.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["sandbox_adapter"], "docker")
        self.assertEqual(run["sandbox_image"], "python:3.12-slim")

    def test_docker_daemon_down_refuses_prepare(self):
        task = task_dict(task_id="demo-down")
        task["sandbox_adapter"] = "docker"
        task_path = os.path.join(self.tmp.name, "task-demo-down.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        self.dt12("init", "--repo", self.repo, "--home", self.home)
        result = self.dt12("prepare", "--home", self.home, "--task", task_path,
                           expect=1, extra_env={"FAKE_DOCKER_INFO_OK": "0"})
        self.assertIn("Docker is unavailable", result.stderr)

    def test_docker_missing_image_refuses_prepare(self):
        task = task_dict(task_id="demo-noimg")
        task["sandbox_adapter"] = "docker"
        task["sandbox_image"] = "missing:latest"
        task_path = os.path.join(self.tmp.name, "task-demo-noimg.json")
        with open(task_path, "w") as fh:
            json.dump(task, fh)
        self.dt12("init", "--repo", self.repo, "--home", self.home)
        result = self.dt12("prepare", "--home", self.home, "--task", task_path,
                           expect=1,
                           extra_env={"FAKE_DOCKER_IMAGE_MISSING": "missing:latest"})
        self.assertIn("not present locally", result.stderr)


REAL_DOCKER_IMAGE = "python:3.12"


@unittest.skipUnless(sandbox.docker_available() and sandbox.docker_image_present(REAL_DOCKER_IMAGE),
                     f"needs a Docker daemon and the {REAL_DOCKER_IMAGE} image")
class RealDockerCase(DockerFlow):
    """The docker adapter end to end against the real daemon."""

    def test_real_docker_end_to_end(self):
        wt = self.prepare_task("demo-real", image=REAL_DOCKER_IMAGE)
        self.run_to_verified("demo-real")
        with open(os.path.join(self.home, "runs", "demo-real.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["sandbox_adapter"], "docker")
        self.assertTrue(sandbox.probes_passed(run["probes"]), run["probes"])
        status = json.loads(self.dt12("status", "--home", self.home, "--task-id", "demo-real").stdout)
        self.assertEqual(status["phase"], "local-verified")
        for dirpath, _, filenames in os.walk(wt):
            for name in filenames:
                self.assertEqual(os.stat(os.path.join(dirpath, name)).st_uid, os.getuid(),
                                 f"{dirpath}/{name} not owned by the host user")


if __name__ == "__main__":
    unittest.main()
