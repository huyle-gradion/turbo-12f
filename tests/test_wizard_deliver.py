"""Tests for bin/t12-wizard `answer` and `deliver`. Fake `t12`, `git`, `gh`
and a fake forge adapter sit in a temporary workspace and log every call;
nothing touches the network, a socket, or the real home directory.

Contract the tests pin down (the implementation must follow it):
- a run is read from <work>/state/runs/gh-N.json (task_id, phase, branch,
  worktree, delivery_adapter, escalations[-1] = {question, answer});
- `answer N` shows the open question, then records the answer with
  `<work>/factory/bin/t12 resume --home <state> --task-id gh-N --answer TEXT`;
- `deliver N` runs, in order and with the run's worktree as cwd:
  `git push origin <branch>`,
  `<python> <work>/factory/adapters/<adapter>.py create-pr --home .. --task-id .. --out F`,
  `<work>/factory/bin/t12 deliver --home .. --task-id .. --adapter .. --report F`;
  the pull request link is the `ref` of the adapter's report.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIZARD = os.path.join(ROOT, "bin", "t12-wizard")
PR = "https://example.invalid/acme/shop/pull/7"

LOGGER = r'''#!{python}
import json, os, sys
name = {name!r}
with open(os.path.join(os.environ["FAKE_STATE"], "log.jsonl"), "a") as f:
    f.write(json.dumps({{"tool": name, "args": sys.argv[1:], "cwd": os.getcwd()}}) + "\n")
fail = os.environ.get("FAKE_FAIL", "")
key = name + (":" + sys.argv[1] if name == "t12" and len(sys.argv) > 1 else "")
if fail and fail == key:
    sys.stderr.write("boom from " + key + "\n")
    sys.exit(1)
{body}
'''

T12_BODY = r'''
if sys.argv[1] in ("board", "status"):
    home = sys.argv[sys.argv.index("--home") + 1]
    rows = []
    for n in sorted(os.listdir(os.path.join(home, "runs"))):
        r = json.load(open(os.path.join(home, "runs", n)))
        rows.append({"task_id": r["task_id"], "phase": r["phase"], "branch": r["branch"],
                     "worktree": r["worktree"], "next": "human: merge", "busy": False})
    print(json.dumps(rows if sys.argv[1] == "board" else rows[0]))
'''

ADAPTER_BODY = r'''
out = sys.argv[sys.argv.index("--out") + 1]
json.dump({"outcome": "confirmed", "ref": %r, "head": "abc"}, open(out, "w"))
print("github confirmed ref=%s")
''' % (PR, PR)

GH_BODY = r'''
if sys.argv[1:3] == ["auth", "token"]:
    print("fake-token")
'''


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.work = os.path.join(self.tmp, "work")
        self.bindir = os.path.join(self.tmp, "bin")
        self.wt = os.path.join(self.work, "state", "worktrees", "gh-7")
        for d in (self.state, self.bindir, os.path.join(self.tmp, "home"), self.wt,
                  os.path.join(self.work, "factory", "adapters"),
                  os.path.join(self.work, "factory", "bin"),
                  os.path.join(self.work, "state", "runs")):
            os.makedirs(d)
        self.install(os.path.join(self.work, "factory", "bin", "t12"), "t12", T12_BODY)
        self.install(os.path.join(self.work, "factory", "adapters", "github.py"), "adapter", ADAPTER_BODY)
        self.install(os.path.join(self.bindir, "git"), "git", "")
        self.install(os.path.join(self.bindir, "gh"), "gh", GH_BODY)
        with open(os.path.join(self.work, "t12-workspace.json"), "w") as f:
            json.dump({"product_repo": "https://github.com/acme/shop.git",
                       "work_dir": self.work, "commit_name": "Ada",
                       "commit_email": "ada@example.com", "rules_branch": "main",
                       "model_cmd": "fake-model"}, f)
        self.run_record("local-verified")

    def install(self, path, name, body):
        with open(path, "w") as f:
            f.write(LOGGER.format(python=sys.executable, name=name, body=body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def run_record(self, phase, escalations=None):
        with open(os.path.join(self.work, "state", "runs", "gh-7.json"), "w") as f:
            json.dump({"task_id": "gh-7", "phase": phase, "branch": "task/gh-7",
                       "worktree": self.wt, "delivery_adapter": "github",
                       "escalations": escalations or []}, f)

    def wizard(self, args, stdin="", **env):
        e = {"PATH": self.bindir + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin",
             "HOME": os.path.join(self.tmp, "home"), "FAKE_STATE": self.state,
             "GITHUB_TOKEN": "fake-token", "GIT_ALLOW_PROTOCOL": "none"}
        e.update(env)
        r = subprocess.run([sys.executable, WIZARD] + args, cwd=self.work, env=e,
                           input=stdin, capture_output=True, text=True, timeout=60)
        return r, r.stdout + r.stderr

    def calls(self):
        path = os.path.join(self.state, "log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def steps(self):
        """Mutating calls only: push, adapter, t12 deliver/resume, and anything gh."""
        out = []
        for c in self.calls():
            if c["tool"] == "t12" and c["args"][:1] in (["board"], ["status"]):
                continue
            if c["tool"] == "gh" and c["args"][:2] == ["auth", "token"]:
                continue
            if c["tool"] == "gh" and c["args"][:2] == ["auth", "status"]:
                continue
            out.append(c)
        return out

    def test_answer_shows_question_and_records_typed_or_flag_answer(self):
        self.run_record("paused", [{"question": "Which currency?", "answer": None}])
        r, out = self.wizard(["answer", "7"], stdin="euros\n")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("Which currency?", out)
        resumes = [c for c in self.steps() if c["tool"] == "t12"]
        self.assertEqual(len(resumes), 1, resumes)
        a = resumes[0]["args"]
        self.assertEqual(a[0], "resume")
        self.assertEqual(a[a.index("--task-id") + 1], "gh-7")
        self.assertEqual(a[a.index("--answer") + 1], "euros")

        shutil.rmtree(self.state)
        os.makedirs(self.state)
        r, out = self.wizard(["answer", "7", "--answer", "dollars"])
        self.assertEqual(r.returncode, 0, out)
        a = [c for c in self.steps() if c["tool"] == "t12"][0]["args"]
        self.assertEqual(a[a.index("--answer") + 1], "dollars")

    def test_deliver_pushes_opens_pr_records_and_prints_link(self):
        r, out = self.wizard(["deliver", "7"])
        self.assertEqual(r.returncode, 0, out)
        steps = self.steps()
        self.assertEqual([c["tool"] for c in steps], ["git", "adapter", "t12"], steps)
        self.assertEqual(steps[0]["args"], ["push", "origin", "task/gh-7"])
        self.assertEqual(steps[1]["args"][0], "create-pr")
        self.assertEqual(steps[2]["args"][0], "deliver")
        self.assertEqual(steps[2]["args"][steps[2]["args"].index("--adapter") + 1], "github")
        for c in steps:
            self.assertEqual(os.path.realpath(c["cwd"]), os.path.realpath(self.wt))
        self.assertIn(PR, r.stdout)

    def test_deliver_refuses_run_that_is_not_local_verified(self):
        self.run_record("green")
        r, out = self.wizard(["deliver", "7"])
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("green", out)
        self.assertEqual(self.steps(), [])

    def test_failed_step_stops_there_and_names_one_retry(self):
        cases = {"git": "git push origin task/gh-7", "adapter": "create-pr", "t12:deliver": "deliver"}
        later = {"git": {"adapter", "t12"}, "adapter": {"t12"}, "t12:deliver": set()}
        for fail, retry in cases.items():
            with self.subTest(fail=fail):
                if os.path.exists(os.path.join(self.state, "log.jsonl")):
                    os.remove(os.path.join(self.state, "log.jsonl"))
                r, out = self.wizard(["deliver", "7"], FAKE_FAIL=fail)
                self.assertNotEqual(r.returncode, 0, out)
                self.assertIn("boom from " + fail, out)
                retries = [ln for ln in out.splitlines() if "retry" in ln.lower()]
                self.assertEqual(len(retries), 1, out)
                self.assertIn(retry, retries[0])
                ran = {c["tool"] for c in self.steps()}
                self.assertFalse(ran & later[fail], ran)
                self.assertNotIn(PR, out if fail != "t12:deliver" else "")

    def test_never_merges_and_never_answers_by_itself(self):
        r, out = self.wizard(["deliver", "7"])
        self.assertEqual(r.returncode, 0, out)
        for c in self.calls():
            self.assertNotIn("merge", " ".join(c["args"]).lower(), c)
        self.assertFalse([c for c in self.calls() if c["tool"] == "gh" and c["args"][:1] == ["pr"]])

        shutil.rmtree(self.state)
        os.makedirs(self.state)
        self.run_record("paused", [{"question": "Which currency?", "answer": None}])
        self.wizard(["answer", "7"], stdin="")
        self.assertEqual([c for c in self.calls() if c["args"][:1] == ["resume"]], [])


if __name__ == "__main__":
    unittest.main()
