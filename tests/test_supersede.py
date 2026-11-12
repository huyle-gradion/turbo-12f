"""Issue #22: `prepare --supersede` must not let the new run overwrite
the archived run's evidence. Drives the real CLI on a synthetic repo.
"""

import glob
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from adapters.common import manifest_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "t12")
TASK = "demo-1"

APP_V1 = 'def greet(name="world"):\n    return "Hello!"\n'
APP_V2 = 'def greet(name="world"):\n    return f"Hello, {name}!"\n'
TEST_FILE = """import unittest
from app import greet

class TestGreet(unittest.TestCase):
    def test_named(self):
        self.assertEqual(greet("Ana"), "Hello, Ana!")
%s
if __name__ == "__main__":
    unittest.main()
"""
EXTRA_TEST = """
    def test_default(self):
        self.assertEqual(greet(), "Hello, world!")
"""


def evidence_refs(value):
    """Every evidence ref (a dict with path, sha256 and provenance) nested in value."""
    if isinstance(value, dict):
        if {"path", "sha256", "provenance"} <= set(value):
            yield value
        for v in value.values():
            yield from evidence_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from evidence_refs(v)


def mismatches(refs):
    bad = []
    for ref in refs:
        try:
            with open(ref["path"], "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            actual = "missing"
        if actual != ref["sha256"]:
            bad.append(f"{ref['name']} at {ref['path']}")
    return bad


class SupersedeEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.repo, "tests"))
        for args in (("init", "-b", "main"), ("config", "user.email", "test@example.com"),
                     ("config", "user.name", "Test")):
            self.git(self.repo, *args)
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write(APP_V1)
        with open(os.path.join(self.repo, "tests", "__init__.py"), "w") as fh:
            fh.write("")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "init")
        self.task_path = os.path.join(self.tmp.name, "task.json")
        with open(self.task_path, "w") as fh:
            json.dump({
                "id": TASK, "title": "Add greet()", "base": "main",
                "acceptance_criteria": ["greet('Ana') returns 'Hello, Ana!'"],
                "time_limit_s": 600, "op_limit": 50, "repair_cap": 2,
                "gates": [{"name": "unit", "command": [
                    "python3", "-m", "unittest", "discover", "-s", "tests", "-q"]}],
            }, fh)
        self.worktree = os.path.join(self.home, "worktrees", TASK)
        self.t12("init", "--repo", self.repo, "--home", self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)}: {result.stderr}")
        return result.stdout.strip()

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, expect,
                         f"t12 {' '.join(args)} -> {result.returncode}: {result.stdout} {result.stderr}")
        return result

    def act(self, role, session, output):
        path = os.path.join(self.tmp.name, f"act-{role}.json")
        with open(path, "w") as fh:
            json.dump(output, fh)
        return self.t12("act", "--home", self.home, "--task-id", TASK, "--role", role,
                        "--session", session, "--input", path)

    def run_to_review(self, extra_test, finding):
        """red, stage, gate, replay and a structured review, all through the CLI."""
        with open(os.path.join(self.worktree, "tests", "test_greet.py"), "w") as fh:
            fh.write(TEST_FILE % extra_test)
        self.act("test-writer", "sess-tw", {"tool": "red_test_ready", "args": {
            "test_path": "tests/test_greet.py",
            "command": ["python3", "-m", "unittest", "tests.test_greet", "-q"],
            "marker": "FAILED"}})
        with open(os.path.join(self.worktree, "app.py"), "w") as fh:
            fh.write(APP_V2)
        self.git(self.worktree, "add", ".")
        self.git(self.worktree, "commit", "-m", "feat: implement greet")
        self.act("builder", "sess-builder", {"tool": "implementation_ready",
                                             "args": {"summary": "implement greet"}})
        self.t12("gate", "--home", self.home, "--task-id", TASK, "--name", "unit",
                 "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", TASK)
        self.act("reviewer", "sess-reviewer", {"tool": "review", "args": {
            "verdict": "merge-ready", "verification": "re-ran the unit gate",
            "findings": [{"id": "R1", "class": finding, "severity": "Nit"}]}})

    def test_superseded_run_keeps_its_evidence(self):
        self.t12("prepare", "--home", self.home, "--task", self.task_path)
        self.run_to_review(EXTRA_TEST, "Style")
        self.t12("stop", "--home", self.home, "--task-id", TASK, "--reason", "bad approach")
        self.t12("prepare", "--home", self.home, "--task", self.task_path, "--supersede")

        runs = os.path.join(self.home, "runs")
        [archived_json] = glob.glob(os.path.join(runs, f"{TASK}.superseded-*.json"))
        [archived_log] = glob.glob(os.path.join(runs, f"{TASK}.superseded-*.events.jsonl"))
        with open(archived_json) as fh:
            archived_refs = list(evidence_refs(json.load(fh)))
        with open(archived_log) as fh:
            archived_refs += list(evidence_refs([json.loads(line) for line in fh if line.strip()]))
        names = {ref["name"] for ref in archived_refs}
        for expected in ("red-output.txt", "gate-unit-output.txt", "replay-output.txt",
                         "review-reviewer-1.md"):
            self.assertIn(expected, names)
        self.assertTrue(any(n.startswith("worker-output-") for n in names), names)

        # The new run writes evidence under the same names, then certifies.
        self.run_to_review("", "Naming")
        self.t12("green", "--home", self.home, "--task-id", TASK)
        self.t12("verify", "--home", self.home, "--task-id", TASK)
        self.t12("report", "--home", self.home)

        with open(os.path.join(runs, f"{TASK}.json")) as fh:
            new_run = json.load(fh)
        with open(os.path.join(runs, f"{TASK}.events.jsonl")) as fh:
            new_refs = list(evidence_refs(new_run)) + list(
                evidence_refs([json.loads(line) for line in fh if line.strip()]))
        self.assertIn("manifest.txt", {ref["name"] for ref in new_refs})
        manifest_ref = [r for r in evidence_refs(new_run) if r["name"] == "manifest.txt"][-1]
        with open(manifest_ref["path"], "rb") as fh:
            manifest_bytes = fh.read()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), manifest_ref["sha256"])
        self.assertEqual(manifest_text(self.home, TASK).encode("utf-8"), manifest_bytes,
                         "delivery does not read the new run's manifest")

        self.assertEqual(mismatches(new_refs), [], "new run evidence does not verify")
        self.assertEqual(mismatches(archived_refs), [],
                         "archived run evidence was overwritten by the superseding run")

        retro = json.loads(self.t12("retro", "--home", self.home, "--json").stdout)
        self.assertEqual(retro["unreadable_reviews"], 0)
        self.assertEqual(retro["reviews"], 2)


if __name__ == "__main__":
    unittest.main()
