"""Tests for bin/t12-wizard `sync`. A fake `git` logs every call (cwd, argv)
in a temporary workspace; no network, socket, or real home directory.

Contract: factory clone is <work>/factory, product clone is <work>/product,
trunk branch is t12-trunk. `sync` runs `git pull ...` in both clones; with
rules_branch "local" it also runs `git rebase ... main` in the product clone;
on a rebase conflict it runs `git rebase --abort`, checks out main last, exits
non-zero and says the product changed under the profile.
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

GIT = '''#!{python}
import json, os, sys
with open(os.path.join(os.environ["FAKE_STATE"], "log.jsonl"), "a") as f:
    f.write(json.dumps({{"args": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
if os.environ.get("FAKE_CONFLICT") and sys.argv[1] == "rebase" and "--abort" not in sys.argv:
    sys.stderr.write("CONFLICT (content): Merge conflict in .t12/project.json\\n")
    sys.exit(1)
'''


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.work = os.path.join(self.tmp, "work")
        self.bindir = os.path.join(self.tmp, "bin")
        for d in (self.state, self.bindir, os.path.join(self.tmp, "home"),
                  os.path.join(self.work, "factory"), os.path.join(self.work, "product")):
            os.makedirs(d)
        git = os.path.join(self.bindir, "git")
        with open(git, "w") as f:
            f.write(GIT.format(python=sys.executable))
        os.chmod(git, os.stat(git).st_mode | stat.S_IXUSR)

    def sync(self, rules, **env):
        with open(os.path.join(self.work, "t12-workspace.json"), "w") as f:
            json.dump({"product_repo": "https://github.com/acme/shop.git",
                       "work_dir": self.work, "commit_name": "Ada",
                       "commit_email": "ada@example.com", "rules_branch": rules}, f)
        e = {"PATH": self.bindir + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin",
             "HOME": os.path.join(self.tmp, "home"), "FAKE_STATE": self.state,
             "GIT_ALLOW_PROTOCOL": "none"}
        e.update(env)
        r = subprocess.run([sys.executable, WIZARD, "sync"], cwd=self.work, env=e,
                           capture_output=True, text=True, timeout=60)
        path = os.path.join(self.state, "log.jsonl")
        calls = []
        if os.path.exists(path):
            with open(path) as f:
                calls = [json.loads(line) for line in f]
        for c in calls:
            c["cwd"] = os.path.realpath(c["cwd"])
        return r, r.stdout + r.stderr, calls

    def in_clone(self, calls, name, verb):
        d = os.path.realpath(os.path.join(self.work, name))
        return [c for c in calls if c["cwd"] == d and verb in c["args"]]

    def test_local_rules_pull_both_and_rebase_trunk_onto_main(self):
        r, out, calls = self.sync("local")
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(self.in_clone(calls, "factory", "pull"), calls)
        self.assertTrue(self.in_clone(calls, "product", "pull"), calls)
        rebases = self.in_clone(calls, "product", "rebase")
        self.assertEqual(len(rebases), 1, calls)
        self.assertTrue(any(a == "main" or a.endswith("/main") for a in rebases[0]["args"]), rebases)

    def test_main_rules_do_no_rebase(self):
        r, out, calls = self.sync("main")
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(self.in_clone(calls, "factory", "pull"), calls)
        self.assertTrue(self.in_clone(calls, "product", "pull"), calls)
        self.assertFalse([c for c in calls if "rebase" in c["args"]], calls)

    def test_rebase_conflict_aborts_returns_to_main_and_explains(self):
        r, out, calls = self.sync("local", FAKE_CONFLICT="1")
        self.assertNotEqual(r.returncode, 0, out)
        argv = [c["args"] for c in calls]
        ri = next(i for i, a in enumerate(argv) if a[:1] == ["rebase"] and "--abort" not in a)
        self.assertTrue(any(a[:2] == ["rebase", "--abort"] for a in argv[ri + 1:]), argv)
        checkouts = [a for a in argv[ri + 1:] if a[:1] == ["checkout"] or a[:1] == ["switch"]]
        self.assertTrue(checkouts and checkouts[-1][-1] == "main", argv)
        low = out.lower()
        self.assertIn("product", low)
        self.assertIn("changed", low)
        self.assertIn("profile", low)


if __name__ == "__main__":
    unittest.main()
