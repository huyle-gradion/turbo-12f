"""Tests for the delivery adapters (Phase 7) against fake GitHub,
GitLab, Bitbucket and Asana servers on localhost. No network, no real credentials.

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
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
        if path == "/repos/o/r":
            if scenario.get("project_missing"):
                return self._json(404, {"message": "Not Found"})
            return self._json(200, {"full_name": "o/r"})
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
            if existing and query.get("base", [existing.get("target", "main")])[0] == existing.get("target", "main"):
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
                                    "head": {"sha": head},
                                    "base": {"ref": scenario.get(
                                        "target", (scenario.get("existing_pr") or {}).get("target", "main"))}})
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


class JsonHandler(BaseHTTPRequestHandler):
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

    def _consume_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else None


class FakeGitLab(JsonHandler):
    PROJECT = "/api/v4/projects/g%2Fsub%2Fn"
    URL = "https://gitlab.example/g/sub/n/-/merge_requests"

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        self.REQUESTS.append(("GET", path))
        scenario = self.SCENARIO
        if path == self.PROJECT:
            if scenario.get("project_missing"):
                return self._json(404, {"message": "404 Project Not Found"})
            return self._json(200, {"path_with_namespace": "g/sub/n"})
        if path == f"{self.PROJECT}/repository/branches/factory%2Fdemo-1":
            if scenario.get("branch_missing"):
                return self._json(404, {"message": "404 Branch Not Found"})
            return self._json(200, {"name": "factory/demo-1", "commit": {"id": CANDIDATE}})
        if path == f"{self.PROJECT}/merge_requests":
            if scenario.get("list_status"):
                return self._json(scenario["list_status"], {"message": "boom"})
            existing = scenario.get("existing")
            if (existing and query.get("source_branch") == ["factory/demo-1"]
                    and query.get("target_branch", [existing.get("target", "main")])
                    == [existing.get("target", "main")]):
                return self._json(200, [{"iid": existing["number"]}])
            return self._json(200, [])
        if path.startswith(f"{self.PROJECT}/merge_requests/"):
            iid = int(path.rsplit("/", 1)[1])
            if scenario.get("readback_status"):
                return self._json(scenario["readback_status"], {"message": "boom"})
            sha = scenario.get("readback_head", CANDIDATE)
            if scenario.get("existing"):
                sha = scenario["existing"]["head"]
            return self._json(200, {"iid": iid, "sha": sha, "web_url": f"{self.URL}/{iid}",
                                    "target_branch": scenario.get("target", "main")})
        return self._json(404, {"message": "404 Not Found"})

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._consume_body()
        self.REQUESTS.append(("POST", path, payload))
        scenario = self.SCENARIO
        if path == f"{self.PROJECT}/merge_requests":
            time.sleep(scenario.get("create_delay", 0))
            status = scenario.get("create_status", 201)
            if status != 201:
                return self._json(status, {"message": "boom"})
            return self._json(201, {"iid": 7, "web_url": f"{self.URL}/7"})
        return self._json(404, {"message": "404 Not Found"})


class FakeBitbucket(JsonHandler):
    REPO = "/2.0/repositories/ws/slug"
    URL = "https://bitbucket.example/ws/slug/pull-requests"

    def _pr(self, number, hash_):
        return {"id": number, "links": {"html": {"href": f"{self.URL}/{number}"}},
                "source": {"commit": {"hash": hash_}},
                "destination": {"branch": {"name": self.SCENARIO.get("target", "main")}}}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = unquote(parsed.path), parse_qs(parsed.query)
        self.REQUESTS.append(("GET", path))
        scenario = self.SCENARIO
        if path == self.REPO:
            if scenario.get("project_missing"):
                return self._json(404, {"type": "error", "error": {"message": "Repository not found"}})
            return self._json(200, {"full_name": "ws/slug"})
        if path == f"{self.REPO}/refs/branches/factory/demo-1":
            if scenario.get("branch_missing"):
                return self._json(404, {"type": "error", "error": {"message": "No branch"}})
            return self._json(200, {"name": "factory/demo-1",
                                    "target": {"hash": scenario.get("branch_tip", CANDIDATE)}})
        if path == f"{self.REPO}/pullrequests":
            if scenario.get("list_status"):
                return self._json(scenario["list_status"], {"type": "error", "error": {"message": "boom"}})
            existing = scenario.get("existing")
            q = query.get("q", [""])[0]
            if (existing and 'source.branch.name="factory/demo-1"' in q
                    and ("destination.branch.name" not in q
                         or f'destination.branch.name="{existing.get("target", "main")}"' in q)):
                return self._json(200, {"values": [{"id": existing["number"]}]})
            return self._json(200, {"values": []})
        if path.startswith(f"{self.REPO}/pullrequests/"):
            number = int(path.rsplit("/", 1)[1])
            if scenario.get("readback_status"):
                return self._json(scenario["readback_status"], {"type": "error", "error": {"message": "boom"}})
            hash_ = scenario.get("readback_head", CANDIDATE[:12])
            if scenario.get("existing"):
                hash_ = scenario["existing"]["head"]
            return self._json(200, self._pr(number, hash_))
        return self._json(404, {"type": "error", "error": {"message": "not found"}})

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._consume_body()
        self.REQUESTS.append(("POST", path, payload))
        scenario = self.SCENARIO
        if path == f"{self.REPO}/pullrequests":
            time.sleep(scenario.get("create_delay", 0))
            status = scenario.get("create_status", 201)
            if status != 201:
                return self._json(status, {"type": "error", "error": {"message": "boom"}})
            return self._json(201, self._pr(7, CANDIDATE[:12]))
        return self._json(404, {"type": "error", "error": {"message": "not found"}})


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

    def run_adapter(self, script, args, token_env, token="fake-token", extra_env=None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
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

    def test_branch_404_on_an_unreadable_repo_is_uncertain(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"branch_missing": True, "project_missing": True})
        self.assertEqual(data["outcome"], "uncertain")

    def test_a_pr_to_another_base_does_not_shadow_delivery(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"existing_pr": {"number": 3, "head": CANDIDATE,
                                                     "target": "release"}})
        self.assertEqual(data["outcome"], "confirmed")

    def test_existing_pr_on_another_base_rejected(self):
        home = make_verified_home(self.tmp.name, {"github_repo": "o/r"})
        data, _ = self.github(home, {"existing_pr": {"number": 3, "head": CANDIDATE},
                                     "target": "release"})
        self.assertEqual(data["outcome"], "rejected")

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


class ForgeContract:
    """The change-request contract shared by the GitLab and Bitbucket
    adapters: the same scenarios, the same outcomes."""

    SCRIPT = None
    COMMAND = None
    TOKEN_ENV = None
    URL_ENV = None
    TASK_FIELD = None
    TASK_TARGET = None
    URL_SUFFIX = ""
    REF_PREFIX = None

    def forge(self, home, scenario=None, extra=None, extra_env=None):
        self.SERVER_CLASS.SCENARIO = scenario or {}
        self.SERVER_CLASS.REQUESTS = []
        out = os.path.join(self.tmp.name, "report.json")
        args = [self.COMMAND, "--home", home, "--task-id", "demo-1", "--out", out]
        args += extra or []
        env = {self.URL_ENV: self.api_base + self.URL_SUFFIX, **(extra_env or {})}
        result = self.run_adapter(self.SCRIPT, args, self.TOKEN_ENV, extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.read_report(out), result

    def home(self, **fields):
        return make_verified_home(self.tmp.name, {self.TASK_FIELD: self.TASK_TARGET, **fields})

    def posts(self):
        return [r for r in self.SERVER_CLASS.REQUESTS if r[0] == "POST"]

    def test_create_confirmed_after_readback(self):
        data, _ = self.forge(self.home())
        self.assertEqual(data["outcome"], "confirmed")
        self.assertEqual(data["head"], CANDIDATE)
        self.assertEqual(data["ref"], f"{self.REF_PREFIX}/7")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.SERVER_CLASS.REQUESTS[-1][0], "GET")

    def test_existing_same_head_confirmed_without_create(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": self.same_head()}})
        self.assertEqual(data["outcome"], "confirmed")
        self.assertEqual(data["ref"], f"{self.REF_PREFIX}/3")
        self.assertEqual(self.posts(), [])

    def test_existing_other_head_rejected(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": OTHER_HEAD}})
        self.assertEqual(data["outcome"], "rejected")
        self.assertIn(OTHER_HEAD[:12], data["detail"])
        self.assertEqual(self.posts(), [])

    def test_branch_not_pushed_rejected(self):
        data, _ = self.forge(self.home(), {"branch_missing": True})
        self.assertEqual(data["outcome"], "rejected")
        self.assertIn("not on the remote", data["detail"])
        self.assertEqual(self.posts(), [])

    def test_branch_404_on_an_unreadable_project_is_uncertain(self):
        data, _ = self.forge(self.home(), {"branch_missing": True, "project_missing": True})
        self.assertEqual(data["outcome"], "uncertain")
        self.assertEqual(self.posts(), [])

    def test_a_pr_to_another_target_does_not_shadow_delivery_to_base(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": self.same_head(),
                                                        "target": "release"}})
        self.assertEqual(data["outcome"], "confirmed")
        self.assertEqual(len(self.posts()), 1)

    def test_existing_same_head_on_another_target_rejected(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": self.same_head()},
                                           "target": "release"})
        self.assertEqual(data["outcome"], "rejected")
        self.assertIn("release", data["detail"])
        self.assertEqual(self.posts(), [])

    def test_create_forbidden_denied(self):
        data, _ = self.forge(self.home(), {"create_status": 403})
        self.assertEqual(data["outcome"], "denied")

    def test_create_definite_4xx_rejected(self):
        data, _ = self.forge(self.home(), {"create_status": 409})
        self.assertEqual(data["outcome"], "rejected")

    def test_create_server_error_uncertain(self):
        data, _ = self.forge(self.home(), {"create_status": 503})
        self.assertEqual(data["outcome"], "uncertain")

    def test_list_forbidden_is_uncertain_not_denied(self):
        data, _ = self.forge(self.home(), {"list_status": 403})
        self.assertEqual(data["outcome"], "uncertain")
        self.assertEqual(self.posts(), [])

    def test_list_unauthorized_is_uncertain_not_rejected(self):
        data, _ = self.forge(self.home(), {"list_status": 401})
        self.assertEqual(data["outcome"], "uncertain")

    def test_readback_server_error_uncertain(self):
        data, _ = self.forge(self.home(), {"readback_status": 500})
        self.assertEqual(data["outcome"], "uncertain")
        self.assertEqual(len(self.posts()), 1)

    def test_readback_other_head_uncertain(self):
        data, _ = self.forge(self.home(), {"readback_head": OTHER_HEAD})
        self.assertEqual(data["outcome"], "uncertain")

    def test_create_timeout_uncertain(self):
        data, _ = self.forge(self.home(), {"create_delay": 3}, ["--timeout", "1"])
        self.assertEqual(data["outcome"], "uncertain")

    def test_missing_token_exits_nonzero_without_report(self):
        home = self.home()
        out = os.path.join(self.tmp.name, "report.json")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop(self.TOKEN_ENV, None)
        result = subprocess.run(
            ["python3", os.path.join(ROOT, "adapters", self.SCRIPT), self.COMMAND,
             "--home", home, "--task-id", "demo-1", "--out", out],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(out))

    def test_no_target_exits_nonzero_without_report(self):
        home = make_verified_home(self.tmp.name)
        out = os.path.join(self.tmp.name, "report.json")
        result = self.run_adapter(
            self.SCRIPT, [self.COMMAND, "--home", home, "--task-id", "demo-1", "--out", out],
            self.TOKEN_ENV)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(out))

    def test_explicit_flag_overrides_task_field(self):
        home = self.home(**{self.TASK_FIELD: "wrong/place"})
        data, _ = self.forge(home, extra=[self.TARGET_FLAG, self.TASK_TARGET])
        self.assertEqual(data["outcome"], "confirmed")

    def same_head(self):
        return CANDIDATE


class GitLabAdapterCase(ForgeContract, AdapterCase):
    SERVER_CLASS = FakeGitLab
    SCRIPT = "gitlab.py"
    COMMAND = "create-mr"
    TOKEN_ENV = "GITLAB_TOKEN"
    URL_ENV = "GITLAB_URL"
    TASK_FIELD = "gitlab_project"
    TASK_TARGET = "g/sub/n"
    TARGET_FLAG = "--project"
    REF_PREFIX = FakeGitLab.URL

    def test_create_posts_branches_and_title(self):
        self.forge(self.home())
        payload = self.posts()[0][2]
        self.assertEqual(payload["source_branch"], "factory/demo-1")
        self.assertEqual(payload["target_branch"], "main")
        self.assertEqual(payload["title"], "Add greet()")
        self.assertIn(CANDIDATE, payload["description"])

    def test_origin_remote_on_gitlab_host_names_the_project(self):
        home = make_verified_home(self.tmp.name)
        product = os.path.join(self.tmp.name, "product")
        os.makedirs(product)
        subprocess.run(["git", "init", "-q", product], check=True)
        subprocess.run(["git", "-C", product, "remote", "add", "origin",
                        f"{self.api_base}/g/sub/n.git"], check=True)
        data, _ = self.forge(home)
        self.assertEqual(data["outcome"], "confirmed")


class BitbucketAdapterCase(ForgeContract, AdapterCase):
    SERVER_CLASS = FakeBitbucket
    SCRIPT = "bitbucket.py"
    COMMAND = "create-pr"
    TOKEN_ENV = "BITBUCKET_TOKEN"
    URL_ENV = "BITBUCKET_URL"
    TASK_FIELD = "bitbucket_repo"
    TASK_TARGET = "ws/slug"
    TARGET_FLAG = "--repo"
    URL_SUFFIX = "/2.0"
    REF_PREFIX = FakeBitbucket.URL

    def same_head(self):
        return CANDIDATE[:12]

    def test_existing_full_hash_also_confirmed(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": CANDIDATE}})
        self.assertEqual(data["outcome"], "confirmed")

    def test_short_hash_prefix_of_other_commit_rejected(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": OTHER_HEAD[:12]}})
        self.assertEqual(data["outcome"], "rejected")

    def test_branch_tip_sharing_the_short_prefix_rejected(self):
        tip = CANDIDATE[:12] + "f" * 28
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": CANDIDATE[:12]},
                                           "branch_tip": tip})
        self.assertEqual(data["outcome"], "rejected")
        self.assertEqual(self.posts(), [])

    def test_hash_shorter_than_twelve_is_uncertain_not_confirmed(self):
        data, _ = self.forge(self.home(), {"existing": {"number": 3, "head": CANDIDATE[:8]}})
        self.assertEqual(data["outcome"], "uncertain")
        self.assertEqual(self.posts(), [])

    def test_create_posts_branches_and_title(self):
        self.forge(self.home())
        payload = self.posts()[0][2]
        self.assertEqual(payload["source"], {"branch": {"name": "factory/demo-1"}})
        self.assertEqual(payload["destination"], {"branch": {"name": "main"}})
        self.assertEqual(payload["title"], "Add greet()")

    def test_origin_remote_on_bitbucket_org_names_the_repo(self):
        home = make_verified_home(self.tmp.name)
        product = os.path.join(self.tmp.name, "product")
        os.makedirs(product)
        subprocess.run(["git", "init", "-q", product], check=True)
        subprocess.run(["git", "-C", product, "remote", "add", "origin",
                        "git@bitbucket.org:ws/slug.git"], check=True)
        data, _ = self.forge(home)
        self.assertEqual(data["outcome"], "confirmed")


class EndToEndCase(unittest.TestCase):
    """One real factory run, then each forge adapter against its fake
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

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def serve(self, handler, scenario):
        handler.SCENARIO = scenario
        handler.REQUESTS = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

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

    def certified_candidate(self, task_fields):
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
                **task_fields,
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
        return candidate

    def deliver_and_check(self, adapter, script, command, token_env, url, ref):
        report = os.path.join(self.tmp.name, "delivery.json")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env[token_env] = "fake"
        cmd = ["python3", os.path.join(ROOT, "adapters", script), command,
               "--home", self.home, "--task-id", "demo-1", "--out", report]
        if adapter == "github":
            cmd += ["--api-base", url]
        else:
            env[{"gitlab": "GITLAB_URL", "bitbucket": "BITBUCKET_URL"}[adapter]] = url
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(report) as fh:
            data = json.load(fh)
        self.assertEqual(data["outcome"], "confirmed")
        result = self.t12("deliver", "--home", self.home, "--task-id", "demo-1",
                          "--adapter", adapter, "--report", report)
        self.assertIn(f"{adapter} confirmed", result.stdout)
        out = self.t12("status", "--home", self.home, "--task-id", "demo-1").stdout
        deliveries = json.loads(out)["deliveries"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["adapter"], adapter)
        self.assertEqual(deliveries[0]["ref"], ref)
        self.assertEqual(self.t12("verify", "--home", self.home,
                                  "--task-id", "demo-1").returncode, 0)

    def test_factory_run_delivers_a_pr(self):
        candidate = self.certified_candidate({"github_repo": "o/r"})
        self.serve(FakeGitHub, {"readback_head": candidate})
        self.deliver_and_check("github", "github.py", "create-pr", "GITHUB_TOKEN",
                               f"http://127.0.0.1:{self.port}", "https://github.example/pr/7")

    def test_factory_run_delivers_a_merge_request(self):
        candidate = self.certified_candidate({"gitlab_project": "g/sub/n", "branch": "factory/demo-1"})
        self.serve(FakeGitLab, {"readback_head": candidate})
        self.deliver_and_check("gitlab", "gitlab.py", "create-mr", "GITLAB_TOKEN",
                               f"http://127.0.0.1:{self.port}", f"{FakeGitLab.URL}/7")

    def test_factory_run_delivers_a_bitbucket_pr(self):
        candidate = self.certified_candidate({"bitbucket_repo": "ws/slug", "branch": "factory/demo-1"})
        self.serve(FakeBitbucket, {"readback_head": candidate[:12], "branch_tip": candidate})
        self.deliver_and_check("bitbucket", "bitbucket.py", "create-pr", "BITBUCKET_TOKEN",
                               f"http://127.0.0.1:{self.port}/2.0", f"{FakeBitbucket.URL}/7")


if __name__ == "__main__":
    unittest.main()
