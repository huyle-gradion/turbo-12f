"""Tests for bin/t12-wizard `setup`. Fake `git`, `gh` and `t12` programs sit
first (and alone) on PATH and log their argv and cwd; no network, no sockets.
"""

import ast
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

FAKE = r'''#!{python}
import json, os, shutil, sys

tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "log.jsonl"), "a") as f:
    f.write(json.dumps({{"tool": tool, "args": args, "cwd": os.getcwd(),
                         "argv0": os.path.abspath(sys.argv[0])}}) + "\n")

def flag(name):
    return os.path.exists(os.path.join(state, name))

def setflag(name, on=True):
    p = os.path.join(state, name)
    if on:
        open(p, "w").close()
    elif os.path.exists(p):
        os.remove(p)

def die(code, msg="boom"):
    sys.stderr.write(msg + "\n")
    sys.exit(code)

fail = os.environ.get("FAKE_FAIL")
if fail and fail in " ".join([tool] + args):
    die(1)

if tool == "gh":
    if os.environ.get("FAKE_NO_AUTH"):
        die(1, "not logged in")
    sys.exit(0)

if tool == "git":
    if args[:1] == ["clone"]:
        dest = os.path.abspath(args[-1])
        if os.path.exists(dest) and os.listdir(dest):
            die(128, "destination path already exists and is not empty")
        os.makedirs(os.path.join(dest, ".git"))
        os.makedirs(os.path.join(dest, "bin"))
        shutil.copy(os.path.abspath(sys.argv[0]), os.path.join(dest, "bin", "t12"))
        os.chmod(os.path.join(dest, "bin", "t12"), 0o755)
    elif args[:1] == ["rev-parse"] and "--verify" in args:
        if any("t12-trunk" in a for a in args) and not flag("trunk"):
            die(128, "fatal: Needed a single revision")
    elif args[:1] in (["checkout"], ["switch"]) and ("-b" in args or "-c" in args):
        if flag("trunk"):
            die(128, "fatal: a branch named 't12-trunk' already exists")
        setflag("trunk")
    elif args[:1] == ["branch"] and any("t12-trunk" in a for a in args):
        if flag("trunk"):
            die(128, "fatal: a branch named 't12-trunk' already exists")
        setflag("trunk")
    elif args[:1] == ["add"]:
        if not flag("committed"):
            setflag("pending")
    elif args[:1] == ["commit"]:
        if not flag("pending"):
            die(1, "nothing to commit")
        setflag("pending", False)
        setflag("committed")
    elif args[:1] == ["diff"] and "--cached" in args:
        if flag("pending"):
            sys.exit(1)
sys.exit(0)
'''

FACTORY_URL = "https://example.invalid/factory.git"
PRODUCT_URL = "https://example.invalid/acme/shop.git"


class WizardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindir = os.path.join(self.tmp, "fakebin")
        self.state = os.path.join(self.tmp, "state")
        self.work = os.path.join(self.tmp, "work")
        self.cwd = os.path.join(self.tmp, "cwd")
        for d in (self.bindir, self.state, self.work, self.cwd,
                  os.path.join(self.tmp, "home")):
            os.makedirs(d)
        for tool in ("git", "gh", "t12"):
            p = os.path.join(self.bindir, tool)
            with open(p, "w") as f:
                f.write(FAKE.format(python=sys.executable))
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.ws = os.path.join(self.work, "t12-workspace.json")

    def flags(self, name="Ada Dev", rules="local"):
        return ["--product-repo", PRODUCT_URL, "--work-dir", self.work,
                "--commit-name", name, "--commit-email", "ada@example.com",
                "--rules-branch", rules, "--factory-repo", FACTORY_URL]

    def run_wizard(self, args, stdin="", cwd=None, **env):
        e = {"PATH": self.bindir, "HOME": os.path.join(self.tmp, "home"),
             "FAKE_STATE": self.state, "GIT_ALLOW_PROTOCOL": "none"}
        e.update(env)
        kw = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
        return subprocess.run(
            [sys.executable, WIZARD] + args, cwd=cwd or self.cwd, env=e,
            capture_output=True, text=True, timeout=60, **kw)

    def log(self):
        path = os.path.join(self.state, "log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    @staticmethod
    def out(r):
        return r.stdout + r.stderr

    def setup_ok(self, **kw):
        r = self.run_wizard(["setup"] + self.flags(), **kw)
        self.assertEqual(r.returncode, 0, self.out(r))
        return r

    # --- criterion 0 and 1 ---
    def test_happy_path_all_flags_no_terminal(self):
        r = self.run_wizard(["setup"] + self.flags(), stdin=None,
                            GH_TOKEN="sekret-token-123")
        self.assertEqual(r.returncode, 0, self.out(r))
        log = self.log()

        clones = [e for e in log if e["tool"] == "git" and e["args"][:1] == ["clone"]]
        urls = [a for e in clones for a in e["args"]]
        self.assertIn(FACTORY_URL, urls)
        self.assertIn(PRODUCT_URL, urls)

        cfg = [e for e in log if e["tool"] == "git" and "config" in e["args"]]
        self.assertTrue(cfg)
        for e in cfg:
            self.assertTrue(e["cwd"].startswith(self.work), e)
            self.assertNotIn("--global", e["args"])
            self.assertNotIn("--system", e["args"])
        self.assertEqual(len({e["cwd"] for e in cfg}), 2)
        for cwd in {e["cwd"] for e in cfg}:
            flat = " ".join(" ".join(e["args"]) for e in cfg if e["cwd"] == cwd)
            self.assertIn("Ada Dev", flat)
            self.assertIn("ada@example.com", flat)

        def first(pred):
            for i, e in enumerate(log):
                if pred(e):
                    return i
            self.fail("step not found in log: %s" % log)

        trunk = first(lambda e: e["tool"] == "git" and any("t12-trunk" in a for a in e["args"])
                      and e["args"][0] in ("checkout", "switch", "branch"))
        add = first(lambda e: e["tool"] == "git" and e["args"][:1] == ["add"])
        commit = first(lambda e: e["tool"] == "git" and e["args"][:1] == ["commit"])
        init = first(lambda e: e["tool"] == "t12" and e["args"][:1] == ["init"])
        label = first(lambda e: e["tool"] == "gh" and "label" in e["args"]
                      and "create" in e["args"])
        self.assertTrue(trunk < add < commit < init < label, log)
        self.assertEqual(log[init]["argv0"],
                         os.path.join(self.work, "factory", "bin", "t12"))

        with open(self.ws) as f:
            raw = f.read()
        data = json.loads(raw)
        self.assertEqual(data["product_repo"], PRODUCT_URL)
        self.assertEqual(data["commit_name"], "Ada Dev")
        self.assertEqual(data["commit_email"], "ada@example.com")
        self.assertEqual(data["rules_branch"], "local")
        self.assertNotIn("sekret-token-123", raw)

    # --- criterion 2 and R2 ---
    def test_failure_stops_prints_retry_and_rerun_converges(self):
        for fail, step_word, later in (("t12 init", "init", ("label",)),
                                       ("label create", "label", ())):
            with self.subTest(fail=fail):
                shutil.rmtree(self.state)
                shutil.rmtree(self.work)
                os.makedirs(self.state)
                os.makedirs(self.work)
                r = self.run_wizard(["setup"] + self.flags(), FAKE_FAIL=fail)
                self.assertNotEqual(r.returncode, 0)
                text = self.out(r)
                self.assertIn(step_word, text)
                self.assertIn("setup", text)
                self.assertIn("--product-repo", text)
                self.assertFalse(os.path.exists(self.ws))
                for e in self.log():
                    if later:
                        self.assertFalse(e["tool"] == "gh" and "label" in e["args"], e)
                n_first = len(self.log())

                r2 = self.run_wizard(["setup"] + self.flags())
                self.assertEqual(r2.returncode, 0, self.out(r2))
                second = self.log()[n_first:]
                for e in second:
                    if e["tool"] == "git":
                        self.assertNotIn(e["args"][:1], (["commit"],), e)
                        self.assertFalse(e["args"][:1] in (["checkout"], ["switch"])
                                         and ("-b" in e["args"] or "-c" in e["args"]), e)
                self.assertTrue(os.path.exists(self.ws))

    # --- criterion 3 ---
    def test_no_forge_login_says_gh_auth_login_and_never_stores_token(self):
        r = self.run_wizard(["setup"] + self.flags(), stdin=None,
                            FAKE_NO_AUTH="1", GH_TOKEN="sekret-token-123")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("gh auth login", self.out(r))
        self.assertNotIn("token:", self.out(r).lower().replace("gh_token", ""))
        if os.path.exists(self.ws):
            with open(self.ws) as f:
                self.assertNotIn("sekret-token-123", f.read())

    # --- criterion 4 ---
    def test_rerun_same_changes_nothing_differing_asks(self):
        self.setup_ok()
        before = os.stat(self.ws).st_mtime_ns
        with open(self.ws) as f:
            raw = f.read()
        n = len(self.log())

        r = self.run_wizard(["setup"] + self.flags(), stdin=None)
        self.assertEqual(r.returncode, 0, self.out(r))
        with open(self.ws) as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(os.stat(self.ws).st_mtime_ns, before)
        for e in self.log()[n:]:
            self.assertFalse(e["tool"] == "git" and e["args"][:1] == ["commit"], e)

        self.run_wizard(["setup"] + self.flags(name="Bob Other"), stdin="n\n")
        with open(self.ws) as f:
            self.assertEqual(f.read(), raw)

        r = self.run_wizard(["setup"] + self.flags(name="Bob Other"), stdin="y\n")
        self.assertEqual(r.returncode, 0, self.out(r))
        with open(self.ws) as f:
            self.assertEqual(json.load(f)["commit_name"], "Bob Other")

    # --- criterion 5 ---
    def test_other_command_without_workspace_says_setup_first(self):
        r = self.run_wizard(["status"], stdin=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("setup first", self.out(r).lower())

    # --- criterion 6 ---
    def test_stdlib_only_and_only_git_and_gh(self):
        with open(WIZARD) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            for n in names:
                self.assertIn(n.split(".")[0], sys.stdlib_module_names, n)
        self.setup_ok()
        for e in self.log():
            self.assertIn(e["tool"], ("git", "gh", "t12"))


if __name__ == "__main__":
    unittest.main()
