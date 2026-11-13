"""Tests for bin/t12-wizard `take` and `status`. Fake `gh`, a fake intake
adapter and a fake `t12 board` sit in a temporary workspace; nothing
touches the network, a socket, or the real home directory.
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

GH = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "log.jsonl"), "a") as f:
    f.write(json.dumps({{"tool": "gh", "args": args}}) + "\n")
if args[:2] == ["auth", "status"]:
    if os.environ.get("FAKE_NO_AUTH"):
        sys.stderr.write("not logged in\n")
        sys.exit(1)
elif args[:2] == ["auth", "token"]:
    if os.environ.get("FAKE_NO_AUTH"):
        sys.exit(1)
    print("tok-123")
sys.exit(0)
'''

ADAPTER = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_STATE"]

def value(flag):
    return args[args.index(flag) + 1] if flag in args else None

repo, label = value("--repo"), value("--label")
cfg = value("--repo-config")
if cfg:
    intake = json.load(open(cfg)).get("intake", {{}})
    repo, label = repo or intake.get("repo"), label or intake.get("label")
with open(os.path.join(state, "log.jsonl"), "a") as f:
    f.write(json.dumps({{"tool": "intake", "args": args, "repo": repo, "label": label}}) + "\n")
print("wrote gh-5: queue/gh-5.json")
print("skipped gh-6: no Acceptance criteria heading with a list")
'''

T12 = r'''#!{python}
import os, sys
if sys.argv[1:2] == ["board"]:
    with open(os.path.join(os.environ["FAKE_STATE"], "board.json")) as f:
        sys.stdout.write(f.read())
    sys.exit(0)
sys.exit(1)
'''


class TakeStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindir = os.path.join(self.tmp, "fakebin")
        self.state = os.path.join(self.tmp, "state")
        self.work = os.path.join(self.tmp, "work")
        for d in (self.bindir, self.state, os.path.join(self.tmp, "home"),
                  os.path.join(self.work, "factory", "adapters"),
                  os.path.join(self.work, "factory", "bin"),
                  os.path.join(self.work, "state", "runs")):
            os.makedirs(d)
        self.install(os.path.join(self.bindir, "gh"), GH)
        self.install(os.path.join(self.work, "factory", "adapters", "github-issues.py"), ADAPTER)
        self.install(os.path.join(self.work, "factory", "bin", "t12"), T12)
        os.symlink(sys.executable, os.path.join(self.bindir, "python3"))
        with open(os.path.join(self.work, "t12-workspace.json"), "w") as f:
            json.dump({"product_repo": "https://example.invalid/acme/shop.git",
                       "work_dir": self.work, "commit_name": "Ada",
                       "commit_email": "ada@example.com", "rules_branch": "main"}, f)

    @staticmethod
    def install(path, template):
        with open(path, "w") as f:
            f.write(template.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def board(self, rows):
        with open(os.path.join(self.state, "board.json"), "w") as f:
            json.dump(rows, f)

    def run_wizard(self, args, **env):
        e = {"PATH": self.bindir, "HOME": os.path.join(self.tmp, "home"),
             "FAKE_STATE": self.state, "GIT_ALLOW_PROTOCOL": "none"}
        e.update(env)
        r = subprocess.run([sys.executable, WIZARD] + args, cwd=self.work, env=e,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        return r, r.stdout + r.stderr

    def log(self):
        path = os.path.join(self.state, "log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    @staticmethod
    def line_for(out, number):
        lines = [ln for ln in out.splitlines() if re.search(rf"(?<!\d){number}(?!\d)", ln)]
        return lines

    # --- criteria 0 and 1 ---
    def test_take_labels_runs_intake_and_reports_each_issue(self):
        r, out = self.run_wizard(["take", "5", "6"])
        self.assertEqual(r.returncode, 0, out)
        log = self.log()
        edits = [c["args"] for c in log if c["tool"] == "gh" and c["args"][:2] == ["issue", "edit"]]
        self.assertEqual(sorted(a[2] for a in edits), ["5", "6"], log)
        for a in edits:
            self.assertEqual(a[a.index("--add-label") + 1], "factory")
            repo_flag = "-R" if "-R" in a else "--repo"
            self.assertEqual(a[a.index(repo_flag) + 1], "acme/shop")
        intakes = [c for c in log if c["tool"] == "intake"]
        self.assertTrue(intakes, log)
        self.assertEqual((intakes[0]["repo"], intakes[0]["label"]), ("acme/shop", "factory"))
        self.assertEqual(intakes[0]["args"][0], "intake")
        self.assertEqual(os.path.realpath(intakes[0]["args"][intakes[0]["args"].index("--home") + 1]),
                         os.path.join(self.work, "state"))
        last_edit = max(i for i, c in enumerate(log) if c["tool"] == "gh" and c["args"][:2] == ["issue", "edit"])
        self.assertLess(last_edit, log.index(intakes[0]))
        five, six = self.line_for(out, 5), self.line_for(out, 6)
        self.assertEqual(len(five), 1, out)
        self.assertEqual(len(six), 1, out)
        self.assertIn("queued", five[0])
        self.assertNotIn("skipped", five[0])
        self.assertIn("skipped", six[0])
        self.assertIn("no Acceptance criteria heading with a list", six[0])

    # --- criteria 2 and 4 of status ---
    def test_status_one_sentence_per_run(self):
        self.board([
            {"task_id": "gh-1", "phase": "local-verified", "next": "human: merge"},
            {"task_id": "gh-2", "phase": "paused", "next": "human: answer 'Which colour?'"},
            {"task_id": "gh-3", "phase": "red", "next": "builder: stage"},
            {"task_id": "gh-4", "phase": "stopped", "next": "stopped: budget spent"},
        ])
        r, out = self.run_wizard(["status"])
        self.assertEqual(r.returncode, 0, out)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 4, out)
        by_id = {n: self.line_for("\n".join(lines), n) for n in (1, 2, 3, 4)}
        for n, found in by_id.items():
            self.assertEqual(len(found), 1, out)
        self.assertIn("deliver", by_id[1][0].lower())
        self.assertIn("Which colour?", by_id[2][0])
        self.assertIn("answer", by_id[2][0].lower())
        self.assertIn("rerun", by_id[3][0].lower())
        self.assertIn("budget spent", by_id[4][0])

    def test_status_with_no_runs_suggests_take(self):
        self.board([])
        r, out = self.run_wizard(["status"])
        self.assertEqual(r.returncode, 0, out)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, out)
        self.assertIn("take", lines[0])

    # --- criterion 4 ---
    def test_take_without_login_explains_and_changes_nothing(self):
        r, out = self.run_wizard(["take", "5"], FAKE_NO_AUTH="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("gh auth login", out)
        for c in self.log():
            self.assertNotEqual(c["tool"], "intake")
            self.assertNotEqual(c["args"][:2], ["issue", "edit"])


if __name__ == "__main__":
    unittest.main()
