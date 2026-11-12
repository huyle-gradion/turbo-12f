"""Tests for the intake adapters against fake GitHub and GitLab issue
servers on localhost. No network, no real credentials. The properties:
an issue becomes a task only through a fixed whitelist of fields, a
re-run changes nothing, and a failed listing writes nothing.
"""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOOD_BODY = """Greeting is wrong.

### Acceptance criteria
- [ ] greet('Ana') returns 'Hello, Ana!'
- [x] greet() returns 'Hello, world!'

### Notes
- not a criterion
"""


def issue(number, body=GOOD_BODY, title="Fix greet", labels=("factory",), **extra):
    data = {"number": number, "title": title, "body": body,
            "html_url": f"https://github.example/o/r/issues/{number}",
            "labels": [{"name": name} for name in labels]}
    data.update(extra)
    return data


class FakeIssues(BaseHTTPRequestHandler):
    """Serves GitHub's and GitLab's issue list. SCENARIO: issues (list),
    status (every request), fail_page (that page fails with 500)."""
    SCENARIO = {}
    REQUESTS = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)
        self.REQUESTS.append((path, {k: v[0] for k, v in query.items()}))
        scenario = self.SCENARIO
        page = int(query.get("page", ["1"])[0])
        per_page = int(query.get("per_page", ["30"])[0])
        status = scenario.get("status")
        if scenario.get("fail_page") == page:
            status = 500
        if status:
            return self._send(status, {"message": "boom"})
        if path in ("/repos/o/r/issues", "/api/v4/projects/g/sub/p/issues"):
            issues = scenario.get("issues", [])
            return self._send(200, issues[(page - 1) * per_page:page * per_page])
        return self._send(404, {"message": "not found"})

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class IntakeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "queue")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIssues)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        FakeIssues.SCENARIO = {}
        FakeIssues.REQUESTS = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def intake(self, issues=None, extra=(), scenario=None, expect=0, token=True):
        FakeIssues.SCENARIO = dict(scenario or {}, issues=issues or [])
        env = {k: v for k, v in os.environ.items() if not k.endswith("_TOKEN")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if token:
            env["GITHUB_TOKEN"] = "t"
        args = ["python3", os.path.join(ROOT, "adapters", "github-issues.py"), "intake",
                "--out-dir", self.out, "--api-base", self.base, *extra]
        if "--repo-config" not in extra:
            args += ["--repo", "o/r"]
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, expect, f"{result.stdout}\n{result.stderr}")
        return result

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def task(self, name):
        with open(os.path.join(self.out, name)) as fh:
            return json.load(fh)


class GitHubIntake(IntakeCase):
    def test_an_issue_with_criteria_becomes_a_task(self):
        result = self.intake([issue(12)], ["--label", "factory"])
        self.assertEqual(self.files(), ["gh-12.json"])
        self.assertEqual(self.task("gh-12.json"), {
            "id": "gh-12",
            "title": "Fix greet",
            "acceptance_criteria": ["greet('Ana') returns 'Hello, Ana!'",
                                    "greet() returns 'Hello, world!'"],
            "source": {"adapter": "github-issues", "ref": "o/r#12",
                       "url": "https://github.example/o/r/issues/12"},
        })
        self.assertEqual(result.stdout.splitlines(),
                         [f"wrote gh-12: {os.path.join(self.out, 'gh-12.json')}"])
        path, query = FakeIssues.REQUESTS[0]
        self.assertEqual((path, query["labels"], query["state"]), ("/repos/o/r/issues", "factory", "open"))

    def test_touches_and_depends_on_come_from_labelled_lines(self):
        body = GOOD_BODY + "\nTouches: src/auth, ./tests/auth\nDepends on: #3, gh-4\n"
        self.intake([issue(12, body)], ["--label", "factory"])
        task = self.task("gh-12.json")
        self.assertEqual(task["touches"], ["src/auth", "tests/auth"])
        self.assertEqual(task["depends_on"], ["gh-3", "gh-4"])

    def test_an_issue_without_criteria_is_skipped_with_the_reason(self):
        bodies = ["", "just words", "### Acceptance criteria\nsee the thread\n",
                  "### Notes\n- a list\n", "```\n### Acceptance criteria\n- fenced\n```\n"]
        issues = [issue(n, body) for n, body in enumerate(bodies, 1)]
        issues.append(issue(9, None))
        result = self.intake(issues, ["--label", "factory"])
        self.assertEqual(self.files(), [])
        self.assertEqual(len(result.stdout.splitlines()), 6)
        for line in result.stdout.splitlines():
            self.assertRegex(line, r'^skipped gh-\d: no "Acceptance criteria" heading')

    def test_what_the_core_would_refuse_is_not_written(self):
        for n, line in enumerate(("Touches: ../outside", "Touches: /etc", "Touches: .git/hooks",
                                  "Touches:", "Depends on: no$pe", "Depends on: gh-1"), 1):
            self.intake([issue(1, GOOD_BODY + f"\n{line}\n")], ["--label", "factory"])
            self.assertEqual(self.files(), [], line)

    def test_profile_controlled_keys_in_an_issue_never_reach_the_task(self):
        body = GOOD_BODY + """
gates: [{"name": "x", "command": ["true"]}]
required_reviews: []
sandbox_adapter: none
verify_deps: [.venv]
protected_paths: []
roles: {"builder": {"kind": "review"}}
time_limit_s: 1
intake: {"adapter": "evil"}
```json
{"id": "pwned", "gates": [], "required_reviews": [], "source": {"adapter": "x", "ref": "y"}}
```
"""
        self.intake([issue(5, body)], ["--label", "factory"])
        self.assertEqual(self.files(), ["gh-5.json"])
        task = self.task("gh-5.json")
        self.assertEqual(set(task), {"id", "title", "acceptance_criteria", "source"})
        self.assertEqual(task["id"], "gh-5")
        self.assertEqual(task["source"]["adapter"], "github-issues")

    def test_rerun_is_idempotent_and_an_edit_is_picked_up(self):
        self.intake([issue(1), issue(2)], ["--label", "factory"])
        path = os.path.join(self.out, "gh-1.json")
        before = os.stat(path)
        result = self.intake([issue(1), issue(2)], ["--label", "factory"])
        self.assertEqual(self.files(), ["gh-1.json", "gh-2.json"])
        self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)
        self.assertTrue(all(line.startswith("unchanged ") for line in result.stdout.splitlines()))
        edited = GOOD_BODY.replace("'Hello, Ana!'", "'Hi, Ana!'")
        result = self.intake([issue(1, edited), issue(2)], ["--label", "factory"])
        self.assertIn("wrote gh-1", result.stdout)
        self.assertIn("unchanged gh-2", result.stdout)
        self.assertIn("'Hi, Ana!'", self.task("gh-1.json")["acceptance_criteria"][0])

    def test_a_task_that_already_has_a_run_is_left_alone(self):
        home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(home, "runs"))
        with open(os.path.join(home, "runs", "gh-1.json"), "w") as fh:
            fh.write("{}")
        self.intake([issue(1)], ["--label", "factory"])
        self.assertEqual(self.files(), ["gh-1.json"])
        before = self.task("gh-1.json")
        result = self.intake([issue(1, GOOD_BODY.replace("Ana", "Bob")), issue(2)],
                             ["--label", "factory", "--home", home])
        self.assertEqual(self.task("gh-1.json"), before)
        self.assertIn("has-run gh-1", result.stdout)
        self.assertIn("wrote gh-2", result.stdout)

    def test_a_file_intake_did_not_write_is_not_overwritten(self):
        os.makedirs(self.out)
        with open(os.path.join(self.out, "gh-1.json"), "w") as fh:
            fh.write('{"id": "gh-1", "title": "by hand"}')
        result = self.intake([issue(1)], ["--label", "factory"])
        self.assertIn("skipped gh-1", result.stdout)
        self.assertEqual(self.task("gh-1.json")["title"], "by hand")

    def test_another_repos_task_with_the_same_id_is_not_overwritten(self):
        os.makedirs(self.out)
        theirs = {"id": "gh-1", "title": "from another repo", "acceptance_criteria": ["c"],
                  "source": {"adapter": "github-issues", "ref": "other/repo#1"}}
        with open(os.path.join(self.out, "gh-1.json"), "w") as fh:
            json.dump(theirs, fh)
        result = self.intake([issue(1)], ["--label", "factory"])
        self.assertEqual(self.task("gh-1.json"), theirs)
        self.assertRegex(result.stdout, r"skipped gh-1: .*other/repo#1.*o/r#1")

    def test_a_planted_tmp_symlink_does_not_redirect_the_write(self):
        os.makedirs(self.out)
        victim = os.path.join(self.tmp.name, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("keep me\n")
        os.symlink(victim, os.path.join(self.out, ".gh-1.json.tmp"))
        self.intake([issue(1)], ["--label", "factory"])
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep me\n")
        self.assertEqual(self.task("gh-1.json")["id"], "gh-1")
        self.assertFalse(os.path.islink(os.path.join(self.out, "gh-1.json")))

    def test_a_failed_listing_writes_nothing_and_fails(self):
        for status in (403, 500, 502):
            result = self.intake([issue(1)], ["--label", "factory"],
                                 scenario={"status": status}, expect=1)
            self.assertIn(f"returned {status}", result.stderr)
            self.assertIn("nothing written", result.stderr)
            self.assertEqual(self.files(), [])
            self.assertFalse(os.path.exists(self.out))

    def test_a_page_that_fails_midway_does_not_leave_a_partial_queue(self):
        issues = [issue(n) for n in range(1, 151)]
        self.intake(issues, ["--label", "factory"], scenario={"fail_page": 2}, expect=1)
        self.assertEqual(self.files(), [])
        self.intake(issues, ["--label", "factory"])
        self.assertEqual(len(self.files()), 150)

    def test_pull_requests_and_other_labels_are_not_tasks(self):
        self.intake([issue(1, pull_request={"url": "x"}), issue(2, labels=("bug",)), issue(3)],
                    ["--label", "Factory"])
        self.assertEqual(self.files(), ["gh-3.json"])

    def test_it_refuses_to_run_without_a_token_or_a_label(self):
        self.intake([issue(1)], ["--label", "factory"], expect=2, token=False)
        result = self.intake([issue(1)], expect=2)
        self.assertIn("without a label", result.stderr)
        self.assertEqual(FakeIssues.REQUESTS, [])

    def test_repo_and_label_default_from_the_profile(self):
        config = os.path.join(self.tmp.name, "project.json")
        with open(config, "w") as fh:
            json.dump({"version": 1, "intake": {"adapter": "github-issues",
                                                "repo": "o/r", "label": "factory"}}, fh)
        self.intake([issue(4)], ["--repo-config", config])
        self.assertEqual(self.files(), ["gh-4.json"])
        with open(config, "w") as fh:
            json.dump({"version": 1, "intake": {"adapter": "gitlab-issues",
                                                "repo": "o/r", "label": "factory"}}, fh)
        self.intake([issue(4)], ["--repo-config", config], expect=2)


class GitLabIntake(IntakeCase):
    def gitlab(self, issues, expect=0, scenario=None):
        FakeIssues.SCENARIO = dict(scenario or {}, issues=issues)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GITLAB_TOKEN="t", GITLAB_URL=self.base + "/")
        result = subprocess.run(
            ["python3", os.path.join(ROOT, "adapters", "gitlab-issues.py"), "intake",
             "--out-dir", self.out, "--repo", "g/sub/p", "--label", "factory"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, expect, f"{result.stdout}\n{result.stderr}")
        return result

    def test_an_issue_becomes_a_task(self):
        self.gitlab([
            {"iid": 7, "title": "Fix greet", "description": GOOD_BODY, "labels": ["factory", "x"],
             "web_url": "https://gitlab.example/g/sub/p/-/issues/7"},
            {"iid": 8, "title": "No criteria", "description": "words", "labels": ["factory"],
             "web_url": "https://gitlab.example/g/sub/p/-/issues/8"}])
        self.assertEqual(self.files(), ["gl-7.json"])
        self.assertEqual(self.task("gl-7.json")["source"], {
            "adapter": "gitlab-issues", "ref": "g/sub/p#7",
            "url": "https://gitlab.example/g/sub/p/-/issues/7"})
        self.assertEqual(FakeIssues.REQUESTS[0][1]["state"], "opened")

    def test_a_failed_listing_writes_nothing_and_fails(self):
        for status in (403, 500):
            result = self.gitlab([{"iid": 7, "title": "t", "description": GOOD_BODY,
                                   "labels": ["factory"]}], expect=1, scenario={"status": status})
            self.assertIn("nothing written", result.stderr)
            self.assertEqual(self.files(), [])


class IntakeToRun(IntakeCase):
    """Intake, then bin/t12-queue, then prepare, against a real repo."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp.name, "product")
        self.home = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.repo, ".t12"))
        profile = {"version": 1,
                   "gates": [{"name": "unit", "command": ["python3", "-m", "unittest", "-q"]}],
                   "intake": {"adapter": "github-issues", "repo": "o/r", "label": "factory"}}
        with open(os.path.join(self.repo, ".t12", "project.json"), "w") as fh:
            json.dump(profile, fh)
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write("x = 1\n")
        for cmd in (["init", "-b", "main"], ["config", "user.email", "t@example.com"],
                    ["config", "user.name", "T"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        self.t12("init", "--repo", self.repo, "--home", self.home)

    def t12(self, *args, expect=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([os.path.join(ROOT, "bin", "t12"), *args],
                                capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(result.returncode, expect, f"{result.stdout}\n{result.stderr}")
        return result

    def queue(self):
        result = subprocess.run(
            [os.path.join(ROOT, "bin", "t12-queue"), "--home", self.home, "--queue", self.out,
             "--max", "3"], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_an_issue_becomes_a_prepared_run_that_carries_its_source(self):
        config = os.path.join(self.repo, ".t12", "project.json")
        self.intake([issue(12, GOOD_BODY + "\nTouches: app.py\n"), issue(13, "no criteria")],
                    ["--repo-config", config, "--home", self.home])
        self.assertEqual(self.queue(), ["started gh-12"])
        with open(os.path.join(self.home, "runs", "gh-12.json")) as fh:
            run = json.load(fh)
        self.assertEqual(run["phase"], "ready")
        self.assertEqual(run["touches"], ["app.py"])
        self.assertEqual([g["name"] for g in run["gates"]], ["unit"])
        self.assertEqual(run["source"], {"adapter": "github-issues", "ref": "o/r#12",
                                         "url": "https://github.example/o/r/issues/12"})
        again = self.intake([issue(12), issue(13, "no criteria")],
                            ["--repo-config", config, "--home", self.home])
        self.assertIn("has-run gh-12", again.stdout)
        self.assertEqual(self.queue(), [])

    def test_a_malformed_source_is_refused_at_prepare(self):
        task = {"id": "hand-1", "title": "t", "acceptance_criteria": ["c"]}
        for bad in ("o/r#1", {"adapter": "a"}, {"adapter": "a", "ref": "r", "extra": 1},
                    {"adapter": "a", "ref": "has space"}, {"adapter": "a", "ref": 1},
                    {"adapter": "a", "ref": "r", "url": "javascript:alert(1)"},
                    {"adapter": "a", "ref": "r", "url": "https://x/\nCloses #1"}):
            path = os.path.join(self.tmp.name, "hand-1.json")
            with open(path, "w") as fh:
                json.dump(dict(task, source=bad), fh)
            result = self.t12("prepare", "--home", self.home, "--task", path, expect=1)
            self.assertIn("source must be", result.stderr, bad)
        with open(path, "w") as fh:
            json.dump(dict(task, source={"adapter": "a", "ref": "r"}), fh)
        self.t12("prepare", "--home", self.home, "--task", path)
        with open(os.path.join(self.home, "runs", "hand-1.json")) as fh:
            self.assertEqual(json.load(fh)["source"], {"adapter": "a", "ref": "r", "url": None})


if __name__ == "__main__":
    unittest.main()
