import fcntl
import json
import os
import subprocess
import tempfile
import time
import unittest

from t12 import model
from test_t12 import APP_V1, APP_V2, BIN, TddFlow, task_dict


class BoardIdleCase(TddFlow, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.repo, "tests"))
        self.flow_write(self.repo, "app.py", APP_V1)
        self.flow_write(self.repo, "tests/__init__.py", "")
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1, "idle_after_s": 60}))
        for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "T"), ("add", "."), ("commit", "-m", "init")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.task_path = os.path.join(self.tmp.name, "task.json")

    def tearDown(self):
        self.tmp.cleanup()

    def t12(self, *args, expect=0, extra_env=None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=120, env=env)
        if expect is not None:
            self.assertEqual(result.returncode, expect,
                             f"t12 {' '.join(args)} -> {result.returncode}: {result.stdout} {result.stderr}")
        return result

    def age_last_event(self, seconds):
        path = os.path.join(self.home, "runs", "demo-1.events.jsonl")
        with open(path) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        events[-1]["ts"] = time.time() - seconds
        with open(path, "w") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True) + "\n")

    def gates_run(self):
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(gates=[{"name": "unit", "command": ["python3", "-c", "pass"]}]), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        self.t12("prepare", "--home", self.home, "--task", self.task_path)
        self.run_tdd(files={"app.py": APP_V2})

    def row(self):
        board = json.loads(self.t12("board", "--home", self.home, "--json").stdout)
        return [r for r in board if r["task_id"] == "demo-1"][0]

    def test_board_reports_whole_seconds_since_the_last_event_for_an_old_run_with_no_runner(self):
        self.gates_run()
        self.age_last_event(90)
        self.assertIn(self.row().get("idle_s"), range(90, 96))

    def test_board_table_row_shows_idle_for_an_old_run_with_no_runner(self):
        self.gates_run()
        self.age_last_event(90)
        table = self.t12("board", "--home", self.home).stdout
        line = [l for l in table.splitlines() if l.startswith("demo-1")][0]
        self.assertIn("idle", line)

    def test_board_reports_no_idle_for_a_run_whose_last_event_is_newer_than_idle_after_s(self):
        self.gates_run()
        self.age_last_event(30)
        self.assertIsNone(self.row().get("idle_s"))

    def test_board_reports_no_idle_for_an_old_run_while_another_process_holds_the_runner_lock(self):
        self.gates_run()
        self.age_last_event(90)
        with open(os.path.join(self.home, "runner.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = self.row()
        self.assertIsNone(row.get("idle_s"))

    def test_board_reports_no_idle_for_an_old_run_while_another_process_holds_the_run_lock(self):
        self.gates_run()
        self.age_last_event(90)
        with model.run_lock(self.home, "demo-1"):
            row = self.row()
        self.assertEqual((row["busy"], row.get("idle_s")), (True, None))


    def test_board_reports_no_idle_for_paused_local_verified_and_stopped_runs_with_an_old_last_event(self):
        self.gates_run()
        self.age_last_event(90)
        run_file = os.path.join(self.home, "runs", "demo-1.json")
        with open(run_file) as fh:
            run = json.load(fh)
        idle = {}
        for phase in ("paused", "local-verified", "stopped"):
            run["phase"] = phase
            with open(run_file, "w") as fh:
                json.dump(run, fh)
            idle[phase] = self.row().get("idle_s")
        self.assertEqual(idle, {"paused": None, "local-verified": None, "stopped": None})

    def test_prepare_refuses_a_profile_with_idle_after_s_zero_naming_the_key(self):
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1, "idle_after_s": 0}))
        for args in (("add", "."), ("commit", "-m", "zero idle")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(gates=[{"name": "unit", "command": ["python3", "-c", "pass"]}]), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        result = self.t12("prepare", "--home", self.home, "--task", self.task_path, expect=None)
        self.assertEqual((result.returncode != 0, "idle_after_s" in result.stderr), (True, True), result.stdout + result.stderr)

    def test_prepare_refuses_a_profile_with_a_negative_idle_after_s_naming_the_key(self):
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1, "idle_after_s": -5}))
        for args in (("add", "."), ("commit", "-m", "negative idle")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(gates=[{"name": "unit", "command": ["python3", "-c", "pass"]}]), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        result = self.t12("prepare", "--home", self.home, "--task", self.task_path, expect=None)
        self.assertEqual((result.returncode != 0, "idle_after_s" in result.stderr), (True, True), result.stdout + result.stderr)

    def test_prepare_refuses_a_profile_with_a_string_idle_after_s_naming_the_key(self):
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1, "idle_after_s": "60"}))
        for args in (("add", "."), ("commit", "-m", "string idle")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(gates=[{"name": "unit", "command": ["python3", "-c", "pass"]}]), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        result = self.t12("prepare", "--home", self.home, "--task", self.task_path, expect=None)
        self.assertEqual((result.returncode != 0, "idle_after_s" in result.stderr), (True, True), result.stdout + result.stderr)

    def test_prepare_refuses_a_profile_with_a_boolean_idle_after_s_naming_the_key(self):
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1, "idle_after_s": True}))
        for args in (("add", "."), ("commit", "-m", "boolean idle")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        with open(self.task_path, "w") as fh:
            json.dump(task_dict(gates=[{"name": "unit", "command": ["python3", "-c", "pass"]}]), fh)
        self.t12("init", "--repo", self.repo, "--home", self.home)
        result = self.t12("prepare", "--home", self.home, "--task", self.task_path, expect=None)
        self.assertEqual((result.returncode != 0, "idle_after_s" in result.stderr), (True, True), result.stdout + result.stderr)

    def test_board_reports_no_idle_for_a_90s_old_run_when_the_profile_omits_idle_after_s(self):
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1}))
        for args in (("add", "."), ("commit", "-m", "omit idle")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.gates_run()
        self.age_last_event(90)
        self.assertIsNone(self.row().get("idle_s"))

    def test_board_reports_idle_for_a_700s_old_run_when_the_profile_omits_idle_after_s(self):
        self.flow_write(self.repo, ".t12/project.json", json.dumps({"version": 1}))
        for args in (("add", "."), ("commit", "-m", "omit idle")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.gates_run()
        self.age_last_event(700)
        self.assertIn(self.row().get("idle_s"), range(700, 706))

    def test_board_reports_no_idle_for_a_90s_old_run_whose_record_has_no_idle_after_s_key(self):
        self.gates_run()
        run_path = os.path.join(self.home, "runs", "demo-1.json")
        with open(run_path) as fh:
            record = json.load(fh)
        del record["idle_after_s"]
        with open(run_path, "w") as fh:
            json.dump(record, fh)
        self.age_last_event(90)
        self.assertIsNone(self.row().get("idle_s"))
