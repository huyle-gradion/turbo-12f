"""The runner delivers local-verified runs to the forge the profile names.

No network and no sockets. The test copies the factory (t12/, bin/,
adapters/) into a temp dir and overwrites the github, gitlab and
bitbucket adapters there with fakes that take the real adapters'
subcommands and write a report. A fake `git` first on PATH records
`git push` and never contacts a remote; every other git call goes to
the real git. tests/fake_model.py is the model, as in test_runner.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from t12 import profile
from t12.errors import StateError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_MODEL = os.path.join(ROOT, "tests", "fake_model.py")
TOKENS = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN", "bitbucket": "BITBUCKET_TOKEN"}

FAKE_ADAPTER = '''#!/usr/bin/env python3
import json, os, sys

name = os.path.basename(__file__)[:-3]
SUBCOMMAND = {"github": "create-pr", "gitlab": "create-mr", "bitbucket": "create-pr"}[name]
TOKEN = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN", "bitbucket": "BITBUCKET_TOKEN"}[name]
argv = sys.argv[1:]
opts = dict(zip(argv[1::2], argv[2::2]))
task_id = opts.get("--task-id")
pushes = []
if os.path.exists(os.environ["FAKE_PUSH_LOG"]):
    with open(os.environ["FAKE_PUSH_LOG"]) as pl:
        pushes = [json.loads(line) for line in pl]
branch = None
run_path = os.path.join(opts.get("--home", ""), "runs", f"{task_id}.json")
if os.path.exists(run_path):
    with open(run_path) as rf:
        branch = json.load(rf)["branch"]
with open(os.environ["FAKE_ADAPTER_LOG"], "a") as fh:
    fh.write(json.dumps({"adapter": name, "argv": argv, "task_id": task_id,
                         "branch_was_pushed": any(branch in p["argv"] for p in pushes)}) + "\\n")
if argv[:1] != [SUBCOMMAND]:
    print(f"error: invalid choice: {argv[:1]}", file=sys.stderr)
    sys.exit(2)
if not os.environ.get(TOKEN):
    print(f"error: {TOKEN} is not set", file=sys.stderr)
    sys.exit(2)
if os.environ.get("FAKE_FAIL") == "adapter" and task_id == "demo-1":
    print("error: forge unreachable", file=sys.stderr)
    sys.exit(1)
with open(run_path) as rf:
    candidate = json.load(rf)["candidate"]["commit"]
head = "0" * 40 if os.environ.get("FAKE_FAIL") == "deliver" and task_id == "demo-1" else candidate
with open(opts["--out"], "w") as out:
    json.dump({"adapter": name, "task_id": task_id,
               "outcome": os.environ.get("FAKE_OUTCOME", "confirmed"), "head": head,
               "ref": f"https://{name}.example.invalid/{task_id}/pull/1", "detail": ""}, out)
print(f"{name} report: {opts['--out']}")
'''

FAKE_GIT = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
if "push" in args:
    with open(os.environ["FAKE_PUSH_LOG"], "a") as fh:
        fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\\n")
    if os.environ.get("FAKE_FAIL") == "push" and "demo-1" in " ".join(args) + os.getcwd():
        print("fatal: unable to access the remote", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
os.execv(os.environ["REAL_GIT"], [os.environ["REAL_GIT"]] + args)
'''


class ProfileCase(unittest.TestCase):
    def parse(self, delivery):
        return profile.parse(json.dumps({"version": 1, "delivery": delivery}))

    def test_the_profile_accepts_change_request_adapters_and_nothing_else(self):
        for adapter in ("github", "gitlab", "bitbucket"):
            self.assertEqual(self.parse({"adapter": adapter})["delivery"], {"adapter": adapter})
        for bad in ("github", ["github"], None, {"adapter": "asana"}, {"adapter": "nope"},
                    {"adapter": "github", "token": "x"}):
            with self.assertRaises(StateError, msg=bad):
                self.parse(bad)
        with self.assertRaises(StateError):
            profile.apply(self.parse({"adapter": "github"}),
                          {"delivery": {"adapter": "gitlab"}})
        with self.assertRaises(StateError):
            profile.apply(profile.parse(None), {"delivery": {"adapter": "gitlab"}})


class RunnerDeliveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        join = lambda *p: os.path.join(self.tmp.name, *p)
        self.repo, self.home, self.queue = join("product"), join("state"), join("queue")
        self.factory, self.fakebin = join("factory"), join("fakebin")
        self.model_log, self.notes = join("model.log"), join("notes.log")
        self.push_log, self.adapter_log = join("push.log"), join("adapter.log")
        for name in ("t12", "bin", "adapters"):
            shutil.copytree(os.path.join(ROOT, name), join("factory", name),
                            ignore=shutil.ignore_patterns("__pycache__"))
        for name in TOKENS:
            self.write(os.path.join(self.factory, "adapters", f"{name}.py"), FAKE_ADAPTER)
        os.makedirs(self.fakebin)
        self.write(os.path.join(self.fakebin, "git"), FAKE_GIT)
        os.makedirs(os.path.join(self.repo, "tests"))
        os.makedirs(self.queue)
        self.write(os.path.join(self.repo, "app.py"),
                   'def greet(name="world"):\n    return "Hello!"\n')
        open(os.path.join(self.repo, "tests", "__init__.py"), "w").close()
        for cmd in (["init", "-qb", "main"], ["config", "user.email", "t@example.com"],
                    ["config", "user.name", "T"],
                    ["remote", "add", "origin", join("no-such-remote.git")],
                    ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)
        subprocess.run([self.t12_bin, "init", "--repo", self.repo, "--home", self.home],
                       check=True, capture_output=True)

    @property
    def t12_bin(self):
        return os.path.join(self.factory, "bin", "t12")

    @staticmethod
    def write(path, text):
        with open(path, "w") as fh:
            fh.write(text)
        os.chmod(path, 0o755)

    def fresh(self):
        self.tmp.cleanup()
        self.setUp()

    def commit_profile(self, **fields):
        os.makedirs(os.path.join(self.repo, ".t12"), exist_ok=True)
        with open(os.path.join(self.repo, ".t12", "project.json"), "w") as fh:
            json.dump(dict({"version": 1}, **fields), fh)
        for cmd in (["add", ".t12"], ["commit", "-qm", "profile"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True)

    def prepare(self, task_id):
        path = os.path.join(self.queue, f"{task_id}.json")
        with open(path, "w") as fh:
            json.dump({
                "id": task_id, "title": "greet by name", "base": "main",
                "acceptance_criteria": ["greet('Ana') returns 'Hello, Ana!'"],
                "time_limit_s": 600, "op_limit": 50, "repair_cap": 2,
                "gates": [{"name": "unit", "command": [
                    "python3", "-m", "unittest", "discover", "-s", "tests", "-q"]}],
            }, fh)
        return subprocess.run([self.t12_bin, "prepare", "--home", self.home, "--task", path],
                              capture_output=True, text=True)

    def drive(self, tokens=("gitlab",), **env):
        base = {k: v for k, v in os.environ.items() if k not in TOKENS.values()}
        base.update(PATH=self.fakebin + os.pathsep + os.environ["PATH"],
                    REAL_GIT=shutil.which("git"), FAKE_LOG=self.model_log,
                    FAKE_PUSH_LOG=self.push_log, FAKE_ADAPTER_LOG=self.adapter_log,
                    PYTHONDONTWRITEBYTECODE="1")
        base.update({TOKENS[t]: "not-a-real-token" for t in tokens})
        base.update(env)
        notify = (f"{sys.executable} -c 'import sys; open(sys.argv[1], \"a\")"
                  f".write(sys.argv[2] + \"\\n\")' {self.notes}")
        result = subprocess.run(
            [sys.executable, os.path.join(self.factory, "adapters", "runner.py"),
             "--home", self.home, "--model-cmd", f"{sys.executable} {FAKE_MODEL}",
             "--notify-cmd", notify],
            capture_output=True, text=True, timeout=300, env=base)
        return result.stdout + result.stderr

    def jsonl(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def run_record(self, task_id="demo-1"):
        with open(os.path.join(self.home, "runs", f"{task_id}.json")) as fh:
            return json.load(fh)

    def notes_lines(self):
        if not os.path.exists(self.notes):
            return []
        with open(self.notes) as fh:
            return fh.read().splitlines()

    def test_a_local_verified_run_is_pushed_delivered_recorded_and_linked_once(self):
        self.commit_profile(delivery={"adapter": "gitlab"})
        self.assertEqual(self.prepare("demo-1").returncode, 0)
        out = self.drive()
        run = self.run_record()
        self.assertEqual(run["phase"], "local-verified")
        pushes, calls = self.jsonl(self.push_log), self.jsonl(self.adapter_log)
        self.assertEqual(len(pushes), 1, pushes)
        self.assertIn(run["branch"], pushes[0]["argv"])
        self.assertEqual([(c["adapter"], c["argv"][0], c["task_id"]) for c in calls],
                         [("gitlab", "create-mr", "demo-1")])
        self.assertTrue(calls[0]["branch_was_pushed"], "the adapter ran before the push")
        self.assertEqual([(d["adapter"], d["outcome"]) for d in run["deliveries"]],
                         [("gitlab", "confirmed")])
        self.assertEqual(run["deliveries"][0]["head"], run["candidate"]["commit"])
        self.assertIn("https://gitlab.example.invalid/demo-1/pull/1", out)
        # A restart finds the confirmed delivery and does nothing more.
        self.drive()
        self.assertEqual(len(self.jsonl(self.push_log)), 1)
        self.assertEqual(len(self.jsonl(self.adapter_log)), 1)
        self.assertEqual(len(self.run_record()["deliveries"]), 1)
        # Only ever a create call: no merge, approve or close.
        for call in self.jsonl(self.adapter_log):
            self.assertTrue(call["argv"][0].startswith("create-"), call)

    def test_without_a_delivery_key_the_run_stops_at_local_verified(self):
        self.commit_profile()
        self.assertEqual(self.prepare("demo-1").returncode, 0)
        out = self.drive()
        run = self.run_record()
        self.assertEqual(run["phase"], "local-verified")
        self.assertIn("ready to deliver by hand", out)
        self.assertEqual(self.jsonl(self.push_log), [])
        self.assertEqual(self.jsonl(self.adapter_log), [])
        self.assertEqual(run.get("deliveries", []), [])

    def test_prepare_refuses_a_bad_delivery_key_in_the_profile(self):
        self.commit_profile(delivery={"adapter": "github"})
        self.assertEqual(self.prepare("demo-0").returncode, 0)
        for bad in ("github", {"adapter": "asana"}, {"adapter": "github", "token": "x"}):
            with self.subTest(bad=bad):
                self.commit_profile(delivery=bad)
                result = self.prepare("demo-1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("delivery", result.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.home, "runs", "demo-1.json")))

    def test_a_failed_step_stops_that_run_prints_the_retry_and_goes_on(self):
        steps = {"push": ("push", "task/demo-1"),
                 "adapter": ("gitlab", "create-mr"),
                 "deliver": ("deliver", "--task-id demo-1")}
        for i, (mode, fragments) in enumerate(steps.items()):
            with self.subTest(mode=mode):
                if i:
                    self.fresh()
                self.commit_profile(delivery={"adapter": "gitlab"})
                for task_id in ("demo-1", "demo-2"):
                    self.assertEqual(self.prepare(task_id).returncode, 0)
                out = self.drive(FAKE_FAIL=mode)
                first, second = self.run_record("demo-1"), self.run_record("demo-2")
                self.assertEqual(first["phase"], "local-verified")
                self.assertEqual([d for d in first.get("deliveries", [])
                                  if d["outcome"] == "confirmed"], [])
                self.assertEqual([d["outcome"] for d in second["deliveries"]], ["confirmed"])
                retry = [line for line in out.splitlines()
                         if "demo-1" in line and all(f in line for f in fragments)]
                self.assertTrue(retry, f"no line names demo-1 and {fragments}:\n{out}")

    def test_an_uncertain_delivery_is_notified_and_never_repeated(self):
        self.commit_profile(delivery={"adapter": "gitlab"})
        self.assertEqual(self.prepare("demo-1").returncode, 0)
        self.drive(FAKE_OUTCOME="uncertain")
        self.assertEqual([d["outcome"] for d in self.run_record()["deliveries"]], ["uncertain"])
        notes = [n for n in self.notes_lines() if "demo-1" in n and "uncertain" in n]
        self.assertTrue(notes, self.notes_lines())
        self.assertTrue(any("--resolves" in n for n in notes), notes)
        self.drive(FAKE_OUTCOME="confirmed")
        self.assertEqual(len(self.jsonl(self.push_log)), 1)
        self.assertEqual(len(self.jsonl(self.adapter_log)), 1)
        self.assertEqual([d["outcome"] for d in self.run_record()["deliveries"]], ["uncertain"])

    def test_without_the_forge_token_the_runner_does_not_deliver_and_names_it(self):
        self.commit_profile(delivery={"adapter": "gitlab"})
        self.assertEqual(self.prepare("demo-1").returncode, 0)
        out = self.drive(tokens=())
        run = self.run_record()
        self.assertEqual(run["phase"], "local-verified")
        self.assertIn("GITLAB_TOKEN", out)
        self.assertEqual(self.jsonl(self.push_log), [])
        self.assertEqual(self.jsonl(self.adapter_log), [])
        self.assertEqual(run.get("deliveries", []), [])


if __name__ == "__main__":
    unittest.main()
