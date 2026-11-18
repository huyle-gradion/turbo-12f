import contextlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREATE_URL = "https://api.github.com/repos/o/r/issues"


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        "job_findings_adapter", os.path.join(ROOT, "adapters", "job-findings.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finding(fingerprint, title):
    return {"fingerprint": fingerprint, "title": title,
            "detail": f"detail of {title}", "url": f"https://example.test/{fingerprint}"}


class JobFindingsTest(unittest.TestCase):
    def test_new_findings_are_filed_as_one_issue_listing_both_titles(self):
        adapter = load_adapter()
        calls = []

        def fake(method, url, token, payload=None):
            calls.append((method, url, payload))
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            with open(os.path.join(home, "jobs", "nightly", "fingerprints.json"), "w") as fh:
                json.dump({}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke"),
                                                 finding("b", "Beta broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        posts = [(m, u, json.dumps(p)) for m, u, p in calls if m == "POST"]
        self.assertEqual(
            [(m, u, "Alpha broke" in p and "Beta broke" in p) for m, u, p in posts],
            [("POST", CREATE_URL, True)])

    def test_filed_fingerprints_are_stored_with_the_created_issue_number(self):
        adapter = load_adapter()

        def fake(method, url, token, payload=None):
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            with open(store_path, "w") as fh:
                json.dump({}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke"),
                                                 finding("b", "Beta broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
            with open(store_path) as fh:
                stored = json.load(fh)
        self.assertEqual(stored, {"a": 9, "b": 9})

    def test_finding_still_open_in_store_is_left_out_of_the_new_issue(self):
        adapter = load_adapter()
        calls = []

        def fake(method, url, token, payload=None):
            calls.append((method, url, payload))
            if method == "GET" and url == CREATE_URL + "/7":
                return 200, {"number": 7, "state": "open"}, None
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            with open(os.path.join(home, "jobs", "nightly", "fingerprints.json"), "w") as fh:
                json.dump({"a": 7}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke"),
                                                 finding("c", "Gamma broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        posts = [(m, u, json.dumps(p)) for m, u, p in calls if m == "POST"]
        self.assertEqual(
            [(m, u, "Gamma broke" in p and "Alpha broke" not in p) for m, u, p in posts],
            [("POST", CREATE_URL, True)])

    def test_finding_whose_issue_was_closed_is_filed_again_and_restored_in_store(self):
        adapter = load_adapter()

        def fake(method, url, token, payload=None):
            if method == "GET" and url == CREATE_URL + "/7":
                return 200, {"number": 7, "state": "closed"}, None
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            with open(store_path, "w") as fh:
                json.dump({"a": 7}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
            with open(store_path) as fh:
                stored = json.load(fh)
        self.assertEqual(stored, {"a": 9})

    def test_closing_keyword_reference_in_detail_does_not_link_the_issue(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        hostile = dict(finding("a", "Alpha broke"), detail="Closes #12")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [hostile]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"```.*?```", "", bodies[0], flags=re.S)
        self.assertNotIn("#12", outside_fences)

    def test_html_comment_opener_in_detail_only_appears_inside_a_code_fence(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        opener = "<" + "!" + "--"
        hostile = dict(finding("a", "Alpha broke"), detail=f"before {opener} hidden")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [hostile]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"```.*?```", "", bodies[0], flags=re.S)
        self.assertNotIn(opener, outside_fences)

    def test_label_option_is_refused_with_exit_2_before_any_fetch(self):
        adapter = load_adapter()
        calls = []

        def fake(method, url, token, payload=None):
            calls.append((method, url))
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                     "--repo", "o/r", "--label", "factory"], fetch=fake)
        self.assertEqual((code, calls), (2, []))

    def test_failed_create_exits_1_and_leaves_the_store_unchanged(self):
        adapter = load_adapter()

        def fake(method, url, token, payload=None):
            return 500, None, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            with open(store_path, "w") as fh:
                json.dump({}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                     "--repo", "o/r"], fetch=fake)
            with open(store_path) as fh:
                stored = json.load(fh)
        self.assertEqual((code, stored), (1, {}))

    def test_no_issue_is_created_when_every_finding_is_still_open(self):
        adapter = load_adapter()
        calls = []

        def fake(method, url, token, payload=None):
            calls.append((method, url))
            if method == "GET" and url == CREATE_URL + "/7":
                return 200, {"number": 7, "state": "open"}, None
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            with open(os.path.join(home, "jobs", "nightly", "fingerprints.json"), "w") as fh:
                json.dump({"a": 7}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        self.assertEqual([c for c in calls if c[0] == "POST"], [])

    def test_transport_failure_on_create_exits_1_and_leaves_the_store_unchanged(self):
        adapter = load_adapter()

        def fake(method, url, token, payload=None):
            return None, None, "transport: timed out"

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            with open(store_path, "w") as fh:
                json.dump({}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            try:
                with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                        contextlib.redirect_stderr(io.StringIO()):
                    code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                         "--repo", "o/r"], fetch=fake)
            except Exception as exc:
                code = repr(exc)
            with open(store_path) as fh:
                stored = json.load(fh)
        self.assertEqual((code, stored), (1, {}))

    def test_closing_keyword_reference_in_title_does_not_link_the_issue(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        hostile = finding("a", "Closes #12")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [hostile]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"```.*?```", "", bodies[0], flags=re.S)
        self.assertNotIn("#12", outside_fences)

    def test_closing_keyword_with_gh_style_reference_in_detail_does_not_close_the_issue(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        hostile = finding("a", "Alpha broke")
        hostile["detail"] = "Closes GH-12"
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [hostile]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"```.*?```", "", bodies[0], flags=re.S)
        self.assertIsNone(re.search(r"closes\s+(GH-|#)?12", outside_fences, re.I))

    def test_label_option_in_equals_form_is_refused_with_exit_2_before_any_fetch(self):
        adapter = load_adapter()
        calls = []

        def fake(method, url, token, payload=None):
            calls.append((method, url))
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                     "--repo", "o/r", "--label=factory"], fetch=fake)
        self.assertEqual((code, calls), (2, []))

    def test_closing_keyword_with_issue_url_in_detail_does_not_close_the_issue(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        issue_url = "https://github.com/o/r/issues/12"
        hostile = dict(finding("a", "Alpha broke"), detail=f"Closes {issue_url}")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [hostile]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"```.*?```", "", bodies[0], flags=re.S)
        self.assertIsNone(re.search(r"closes\s+" + re.escape(issue_url), outside_fences, re.I))

    def test_store_is_created_when_the_job_directory_does_not_exist_yet(self):
        adapter = load_adapter()

        def fake(method, url, token, payload=None):
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke")]}, fh)
            code = None
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                         "--repo", "o/r"], fetch=fake)
                except OSError as error:
                    code = type(error).__name__
            stored = None
            if os.path.exists(store_path):
                with open(store_path) as fh:
                    stored = json.load(fh)
        self.assertEqual((code, stored), (0, {"a": 9}))


    def test_html_comment_opener_in_title_sits_inside_a_fence_that_starts_its_own_line(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        opener = "<" + "!" + "--"
        hostile = dict(finding("a", f"Alpha {opener} hidden"), detail="plain detail")
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [hostile]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"^(`{3,}).*?^\1\s*$", "", bodies[0], flags=re.S | re.M)
        self.assertNotIn(opener, outside_fences)

    def test_every_fetch_call_passes_the_payload_argument_positionally(self):
        adapter = load_adapter()
        posts = []

        def fake(method, url, token, payload):
            if method == "GET" and url == CREATE_URL + "/7":
                return 200, {"number": 7, "state": "open"}, None
            posts.append((method, "Gamma broke" in json.dumps(payload)))
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            with open(os.path.join(home, "jobs", "nightly", "fingerprints.json"), "w") as fh:
                json.dump({"a": 7}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha broke"),
                                                 finding("c", "Gamma broke")]}, fh)
            outcome = None
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    outcome = adapter.main(["file", "--home", home, "--findings", findings_path,
                                            "--repo", "o/r"], fetch=fake)
                except TypeError as error:
                    outcome = f"TypeError: {error}"
        self.assertEqual((outcome, posts), (0, [("POST", True)]))

    def test_finding_whose_stored_issue_is_gone_is_filed_again_and_mapped_to_the_new_number(self):
        adapter = load_adapter()
        posts = []

        def fake(method, url, token, payload=None):
            if method == "GET" and url == CREATE_URL + "/7":
                return 404, {"message": "Not Found"}, None
            posts.append((method, "Alpha" in json.dumps(payload)))
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            with open(store_path, "w") as fh:
                json.dump({"a": 7}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                     "--repo", "o/r"], fetch=fake)
            with open(store_path) as fh:
                stored = json.load(fh)
        self.assertEqual((code, posts, stored), (0, [("POST", True)], {"a": 9}))

    def test_finding_whose_stored_issue_was_deleted_with_410_is_filed_again_and_mapped_to_the_new_number(self):
        adapter = load_adapter()
        posts = []

        def fake(method, url, token, payload=None):
            if method == "GET" and url == CREATE_URL + "/7":
                return 410, {"message": "This issue was deleted"}, None
            posts.append((method, "Alpha" in json.dumps(payload)))
            return 201, {"number": 9}, None

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            store_path = os.path.join(home, "jobs", "nightly", "fingerprints.json")
            with open(store_path, "w") as fh:
                json.dump({"a": 7}, fh)
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": [finding("a", "Alpha")]}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = adapter.main(["file", "--home", home, "--findings", findings_path,
                                     "--repo", "o/r"], fetch=fake)
            with open(store_path) as fh:
                stored = json.load(fh)
        self.assertEqual((code, posts, stored), (0, [("POST", True)], {"a": 9}))

    def test_no_line_outside_a_fence_starts_a_raw_html_block_that_could_swallow_a_later_fence(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        opener = "<" + "!" + "--"
        findings = [
            dict(finding("a", "Alpha"), detail="<div>"),
            dict(finding("b", "Beta"), detail=f"x {opener} hidden"),
            dict(finding("c", "Gamma"), detail="visible"),
        ]
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": findings}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"^(`{3,}).*?^\1\s*$", "", bodies[0], flags=re.S | re.M)
        html_starts = [line for line in outside_fences.splitlines() if line.startswith("<")]
        self.assertEqual(html_starts, [])

    def test_title_that_is_an_html_tag_does_not_start_a_raw_html_block_outside_a_fence(self):
        adapter = load_adapter()
        bodies = []

        def fake(method, url, token, payload=None):
            if method == "POST":
                bodies.append(payload["body"])
            return 201, {"number": 9}, None

        opener = "<" + "!" + "--"
        findings = [
            dict(finding("a", "<div>"), detail="plain"),
            dict(finding("b", "Beta"), detail=f"x {opener} hidden"),
        ]
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "jobs", "nightly"))
            findings_path = os.path.join(home, "findings.json")
            with open(findings_path, "w") as fh:
                json.dump({"job": "nightly", "kind": "command", "commit": "0123456789abcdef",
                           "at": 0, "findings": findings}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                adapter.main(["file", "--home", home, "--findings", findings_path,
                              "--repo", "o/r"], fetch=fake)
        outside_fences = re.sub(r"^(`{3,}).*?^\1\s*$", "", bodies[0], flags=re.S | re.M)
        html_starts = [line for line in outside_fences.splitlines() if line.startswith("<")]
        self.assertEqual(html_starts, [])

if __name__ == "__main__":
    unittest.main()
