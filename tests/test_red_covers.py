"""gh-41: `red` takes a `--covers` map from each acceptance criterion
index to the test(s) that check it, records it as evidence, and refuses
a map that leaves any criterion out."""

import json
import os
import subprocess
import tempfile
import unittest

from tests.test_t12 import APP_V1, BIN, TEST_FILE, task_dict


class RedCoversCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(repo, "tests"))
        with open(os.path.join(repo, "app.py"), "w") as fh:
            fh.write(APP_V1)
        open(os.path.join(repo, "tests", "__init__.py"), "w").close()
        for args in (["init", "-b", "main"],
                     ["config", "user.email", "test@example.com"],
                     ["config", "user.name", "Test"],
                     ["add", "."], ["commit", "-m", "init"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        task_path = os.path.join(self.tmp.name, "task.json")
        with open(task_path, "w") as fh:
            json.dump(task_dict(), fh)
        self.t12("init", "--repo", repo, "--home", self.home)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        with open(os.path.join(self.home, "worktrees", "demo-1", "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE)

    def tearDown(self):
        self.tmp.cleanup()

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, expect, f"t12 {args} -> {result.stdout} {result.stderr}")
        return result

    def red(self, covers, expect=0):
        return self.t12(
            "red", "--home", self.home, "--task-id", "demo-1",
            "--test-path", "tests/test_greet.py",
            "--command", "python3 -m unittest tests.test_greet -q",
            "--marker", "FAILED", "--session", "sess-test-writer",
            "--covers", json.dumps(covers), expect=expect,
        )

    def run_record(self):
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            return json.load(fh)

    def test_red_refuses_a_map_missing_a_criterion(self):
        result = self.red({"0": ["test_named"]}, expect=1)
        self.assertIn("1", result.stderr)
        self.assertIn("missing", result.stderr.lower())
        self.assertEqual(self.run_record()["phase"], "ready")

    def test_red_records_the_covers_map(self):
        covers = {"0": ["test_named"], "1": ["test_default", "test_named"]}
        self.red(covers)
        self.assertEqual(self.run_record()["red"]["covers"], covers)


if __name__ == "__main__":
    unittest.main()
