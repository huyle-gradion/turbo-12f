"""Tests for the delivery adapters (Phase 7) against fake GitHub and
Asana servers on localhost. No network, no real credentials.

The scenarios encode the outcome rules, including the two lessons
from the original Turbo's integration work: a 401/403 or 5xx on a
READ is uncertain (state unknown), never denied or rejected; and a
5xx during confirmation is uncertain, never rejected.
"""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = "/home/hatch/workspace/turbo-12f"
CANDIDATE = "a" * 40
OTHER_HEAD = "b" * 40


def make_verified_home(tmp, adapter_fields=None):
    """A state home with a fabricated local-verified run. The adapters
    only read the run record + manifest; the core's chain checks are
    covered by test_delivery.py."""
    home = os.path.join(tmp, "state")
    run = {
        "task_id": "demo-1",
        "title": "Add greet()",
        "acceptance_criteria": ["greet('Ana') returns 'Hello, Ana!'"],
        "base": "main",
        "branch": "factory/demo-1",
        "repo": os.path.join(tmp, "product"),
        "candidate": {"commit": CANDIDATE},
        "phase": "local-verified",
    }
    run.update(adapter_fields or {})
    os.makedirs(os.path.join(home, "runs"))
    os.makedirs(os.path.join(home, "evidence", "demo-1"))
    with open(os.path.join(home, "runs", "demo-1.json"), "w") as fh:
        json.dump(run, fh)
    with open(os.path.join(home, "evidence", "demo-1", "manifest.txt"), "w") as fh:
        fh.write("turbo-12f manifest — task demo-1\ncandidate commit: " + CANDIDATE + "\n")
    return home


class FakeGitHub(BaseHTTPRequestHandler):
    SCENARIO = {}
    REQUESTS = []

    def log_message(self, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, query = self._route()
        self.REQUESTS.append(("GET", path))
        scenario = self.SCENARIO
        if path.startswith("/repos/o/r/branches/"):
            if scenario.get("branch_missing"):
                return self._json(404, {"message": "Branch not found"})
            if scenario.get("branch_status"):
                return self._json(scenario["branch_status"], {"message": "boom"})
            return self._json(200, {"name": "factory/demo-1", "commit": {"sha": CANDIDATE}})
        if path == "/repos/o/r/pulls":
            if scenario.get("list_status"):
                return self._json(scenario["list_status"], {"message": "boom"})
            existing = scenario.get("existing_pr")
            if existing:
                return self._json(200, [{"number": existing["number"],
                                         "html_url": f"https://github.example/pr/{existing['number']}"}])
            return self._json(200, [])
        if path.startswith("/repos/o/r/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            if scenario.get("readback_status"):
                return self._json(scenario["readback_status"], {"message": "boom"})
            head = scenario.get("readback_head", CANDIDATE)
            if scenario.get("existing_pr"):
                head = scenario["existing_pr"]["head"]
            return self._json(200, {"number": number,
                                    "html_url": f"https://github.example/pr/{number}",
                                    "head": {"sha": head}})
        return self._json(404, {"message": "not found"})

    def do_POST(self):
        path, _ = self._route()
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.REQUESTS.append(("POST", path))
        scenario = self.SCENARIO
        if path == "/repos/o/r/pulls":
            status = scenario.get("create_status", 201)
            if status != 201:
                return self._json(status, {"message": scenario.get("create_message", "boom")})
            return self._json(201, {"number": 7, "html_url": "https://github.example/pr/7"})
        return self._json(404, {"message": "not found"})


class FakeAsana(BaseHTTPRequestHandler):
    SCENARIO = {}
    REQUESTS = []

    def log_message(self, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        path = urlparse(self.path).path
        self.REQUESTS.append(("GET", path))
        scenario = self.SCENARIO
        if path == "/tasks/123":
            if scenario.get("task_missing"):
                return self._json(404, {"errors": [{"message": "task: Not Found"}]})
            if scenario.get("read_status"):
                return self._json(scenario["read_status"], {"errors": [{"message": "boom"}]})
            completed = scenario.get("readback_completed", scenario.get("completed", False))
            return self._json(200, {"data": {"gid": "123", "name": "Add greet()",
                                             "completed": completed,
                                             "permalink_url": "https://asana.example/123"}})
        return self._json(404, {"errors": [{"message": "not found"}]})

    def do_POST(self):
        path = urlparse(self.path).path
        self._read_body()
        self.REQUESTS.append(("POST", path))
        scenario = self.SCENARIO
        if path == "/tasks/123/stories":
            status = scenario.get("comment_status", 201)
            return self._json(status, {"data": {"gid": "456"}} if status == 201
                              else {"errors": [{"message": "boom"}]})
        return self._json(404, {"errors": [{"message": "not found"}]})

    def do_PUT(self):
        path = urlparse(self.path).path
        self._read_body()
        self.REQUESTS.append(("PUT", path))
        scenario = self.SCENARIO
        if path == "/tasks/123":
            status = scenario.get("complete_status", 200)
            if status != 200:
                return self._json(status, {"errors": [{"message": "boom"}]})
            scenario["completed"] = True
            return self._json(200, {"data": {"gid": "123", "completed": True}})
        return self._json(404, {"errors": [{"message": "not found"}]})


class AdapterCase(unittest.TestCase):
    SERVER_CLASS = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.SERVER_CLASS)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.SERVER_CLASS.SCENARIO = {}
        self.SERVER_CLASS.REQUESTS = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    @property
    def api_base(self):
        return f"http://127.0.0.1:{self.port}"

    def run_adapter(self, script, args, token_env, token="fake-token"):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env[token_env] = token
        result = subprocess.run(
            ["python3", os.path.join(ROOT, "adapters", script), *args],
            capture_output=True, text=True, timeout=60, env=env)
        return result

    def read_report(self, path):
        with open(path) as fh:
            return json.load(fh)


class GitHubAdapterCase(AdapterCase):
    SERVER_CLASS = FakeGitHub

    def github(self, home, scenario=None, extra=None):
        FakeGitHub.SCENARIO = scenario or {}
        FakeGitHub.REQUESTS = []
        out = os.path.join(self.tmp.name, "report.json")
        args = ["create-pr", "--home", home, "--task-id", "demo-1",
                "--repo", "o/r", "--out", out, "--api-base", self.api_base]
        args += extra or []
        result = self.run_adapter("github.py", args, "GITHUB_TOKEN")
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.read_report(out), result

    def test_create_pr_confirmed(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home)
        self.assertEqual(data["outcome"], "confirmed")
        self.assertEqual(data["head"], CANDIDATE)
        self.assertEqual(data["ref"], "https://github.example/pr/7")
        posts = [r for r in FakeGitHub.REQUESTS if r[0] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_existing_pr_matching_head_confirmed_without_create(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"existing_pr": {"number": 3, "head": CANDIDATE}})
        self.assertEqual(data["outcome"], "confirmed")
        self.assertEqual(data["ref"], "https://github.example/pr/3")
        posts = [r for r in FakeGitHub.REQUESTS if r[0] == "POST"]
        self.assertEqual(posts, [])

    def test_existing_pr_different_head_rejected(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"existing_pr": {"number": 3, "head": OTHER_HEAD}})
        self.assertEqual(data["outcome"], "rejected")
        self.assertIn(OTHER_HEAD[:12], data["detail"])

    def test_branch_not_pushed_rejected(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"branch_missing": True})
        self.assertEqual(data["outcome"], "rejected")
        self.assertIn("not on the remote", data["detail"])

    def test_create_forbidden_denied(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"create_status": 403})
        self.assertEqual(data["outcome"], "denied")

    def test_create_server_error_uncertain(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"create_status": 500})
        self.assertEqual(data["outcome"], "uncertain")

    def test_readback_failure_uncertain(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"readback_status": 500})
        self.assertEqual(data["outcome"], "uncertain")

    def test_list_forbidden_is_uncertain_not_denied(self):
        # The Turbo lesson: a 403 on a READ means the state is unknown,
        # not that the delivery was denied.
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"list_status": 403})
        self.assertEqual(data["outcome"], "uncertain")

    def test_missing_token_exits_nonzero_without_report(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        out = os.path.join(self.tmp.name, "report.json")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("GITHUB_TOKEN", None)
        result = subprocess.run(
            ["python3", os.path.join(ROOT, "adapters", "github.py"), "create-pr",
             "--home", home, "--task-id", "demo-1", "--repo", "o/r",
             "--out", out, "--api-base", self.api_base],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(out))


class AsanaAdapterCase(AdapterCase):
    SERVER_CLASS = FakeAsana

    def asana(self, home, scenario=None):
        FakeAsana.SCENARIO = scenario or {}
        FakeAsana.REQUESTS = []
        out = os.path.join(self.tmp.name, "report.json")
        result = self.run_adapter("asana.py",
                                  ["complete", "--home", home, "--task-id", "demo-1",
                                   "--task", "123", "--out", out,
                                   "--api-base", self.api_base],
                                  "ASANA_TOKEN")
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.read_report(out), result

    def test_complete_confirmed(self):
        home = make_verified_home(self.tmp.name, {"asana_task_gid": "123"})
        data, _ = self.asana(home)
        self.assertEqual(data["outcome"], "confirmed")
        self.assertEqual(data["ref"], "https://asana.example/123")
        puts = [r for r in FakeAsana.REQUESTS if r[0] == "PUT"]
        self.assertEqual(len(puts), 1)

    def test_already_complete_confirmed_without_mutation(self):
        home = make_verified_home(self.tmp.name, {"asana_task_gid": "123"})
        data, _ = self.asana(home, {"completed": True})
        self.assertEqual(data["outcome"], "confirmed")
        mutations = [r for r in FakeAsana.REQUESTS if r[0] in ("POST", "PUT")]
        self.assertEqual(mutations, [])

    def test_complete_forbidden_denied(self):
        home = make_verified_home(self.tmp.name, {"asana_task_gid": "123"})
        data, _ = self.asana(home, {"complete_status": 403})
        self.assertEqual(data["outcome"], "denied")

    def test_read_server_error_uncertain(self):
        home = make_verified_home(self.tmp.name, {"asana_task_gid": "123"})
        data, _ = self.asana(home, {"read_status": 500})
        self.assertEqual(data["outcome"], "uncertain")

    def test_task_missing_rejected(self):
        home = make_verified_home(self.tmp.name, {"asana_task_gid": "123"})
        data, _ = self.asana(home, {"task_missing": True})
        self.assertEqual(data["outcome"], "rejected")

    def test_readback_still_incomplete_uncertain(self):
        home = make_verified_home(self.tmp.name, {"asana_task_gid": "123"})
        data, _ = self.asana(home, {"readback_completed": False})
        self.assertEqual(data["outcome"], "uncertain")


class EndToEndCase(unittest.TestCase):
    """One real factory run, then the GitHub adapter against the fake
    server, then `t12 deliver` recording the confirmed outcome."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.repo, "tests"))
        self._git(self.repo, "init", "-b", "main", "-q")
        self._git(self.repo, "config", "user.email", "t@example.com")
        self._git(self.repo, "config", "user.name", "T")
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write('def greet(name="world"):\n    return "Hello!"\n')
        open(os.path.join(self.repo, "tests", "__init__.py"), "w").close()
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-qm", "init")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        FakeGitHub.SCENARIO = {}
        FakeGitHub.REQUESTS = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            ["python3", "-m", "t12.cli", *args], cwd=ROOT,
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, expect,
                         f"t12 {' '.join(args)} -> {result.returncode}: {result.stderr}")
        return result

    def test_factory_run_delivers_a_pr(self):
        wt = os.path.join(self.home, "worktrees", "demo-1")
        self.t12("init", "--repo", self.repo, "--home", self.home)
        task_path = os.path.join(self.tmp.name, "task.json")
        with open(task_path, "w") as fh:
            json.dump({
                "id": "demo-1", "title": "Add greet()",
                "acceptance_criteria": ["greet('Ana') returns 'Hello, Ana!'"],
                "base": "main", "time_limit_s": 600, "op_limit": 50, "repair_cap": 2,
                "gates": [{"name": "unit", "command": ["python3", "-m", "unittest",
                                                       "discover", "-s", "tests", "-q"]}],
                "github_repo": "o/r",
            }, fh)
        self.t12("prepare", "--home", self.home, "--task", task_path)
        test_file = ('import unittest\nfrom app import greet\n\n'
                     'class TestGreet(unittest.TestCase):\n'
                     '    def test_named(self):\n'
                     '        self.assertEqual(greet("Ana"), "Hello, Ana!")\n')
        with open(os.path.join(wt, "tests", "test_greet.py"), "w") as fh:
            fh.write(test_file)
        self.t12("red", "--home", self.home, "--task-id", "demo-1",
                 "--test-path", "tests/test_greet.py",
                 "--command", "python3 -m unittest tests.test_greet -q",
                 "--marker", "FAILED", "--session", "sess-test-writer")
        with open(os.path.join(wt, "app.py"), "w") as fh:
            fh.write('def greet(name="world"):\n    return f"Hello, {name}!"\n')
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "feat: implement greet")
        self.t12("stage", "--home", self.home, "--task-id", "demo-1", "--session", "sess-builder")
        self.t12("gate", "--home", self.home, "--task-id", "demo-1",
                 "--name", "unit", "--session", "sess-qa")
        self.t12("replay", "--home", self.home, "--task-id", "demo-1")
        review = os.path.join(self.tmp.name, "review.md")
        with open(review, "w") as fh:
            fh.write("Verdict: approve\n")
        self.t12("review", "--home", self.home, "--task-id", "demo-1",
                 "--role", "reviewer", "--session", "sess-reviewer",
                 "--verdict", "approve", "--report", review)
        self.t12("green", "--home", self.home, "--task-id", "demo-1")
        self.t12("manifest", "--home", self.home, "--task-id", "demo-1")
        with open(os.path.join(self.home, "runs", "demo-1.json")) as fh:
            candidate = json.load(fh)["candidate"]["commit"]
        # The fake server answers with the real candidate as the PR head.
        FakeGitHub.SCENARIO = {"readback_head": candidate}

        report = os.path.join(self.tmp.name, "delivery.json")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GITHUB_TOKEN="fake")
        result = subprocess.run(
            ["python3", os.path.join(ROOT, "adapters", "github.py"), "create-pr",
             "--home", self.home, "--task-id", "demo-1", "--out", report,
             "--api-base", f"http://127.0.0.1:{self.port}"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(report) as fh:
            data = json.load(fh)
        self.assertEqual(data["outcome"], "confirmed")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", "github", "--report", report)
        self.assertIn("github confirmed", result.stdout)
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        deliveries = json.loads(out)["deliveries"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["ref"], "https://github.example/pr/7")
        self.assertEqual(self.t12("verify", "--home", self.home,
                                  "--task-id", "demo-1").returncode, 0)


if __name__ == "__main__":
    unittest.main()

