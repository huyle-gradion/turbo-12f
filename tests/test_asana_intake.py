"""gh-48: the Asana tasks intake adapter (`adapters/asana-tasks.py`).

No socket is opened: `common.request` is replaced by a fake Asana that
answers from memory. Decisions this test assumes (from the issue's
answers): the approval marker is a tag (`--tag` / `intake.tag`), the
project is `--project` / `intake.project`, the task id is `as-<gid>`,
the source ref is `<project>#<gid>`, and each sub-task is its own
queued task, and a tagged parent with open sub-tasks is refused.
"""

import ast
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS = os.path.join(ROOT, "adapters")
sys.path.insert(0, ADAPTERS)
import common  # noqa: E402

sys.path.insert(0, ROOT)
from t12 import profile  # noqa: E402

MD = ("Acceptance criteria\n- [ ] greet('Ana') returns 'Hello, Ana!'\n"
      "- [x] greet() returns 'Hello, world!'\nTouches: greet.py\n")
CRITERIA = ["greet('Ana') returns 'Hello, Ana!'", "greet() returns 'Hello, world!'"]
HTML = ("<body>Greeting is wrong.<h2>Acceptance criteria</h2><ul>"
        "<li>greet('Ana') returns 'Hello, Ana!'</li>"
        "<li>greet() returns 'Hello, world!'</li></ul>Touches: greet.py</body>")


def atask(gid, name="Fix greet", html=HTML, tags=("factory",), completed=False, **extra):
    data = {"gid": gid, "name": name, "notes": "", "html_notes": html, "completed": completed,
            "permalink_url": f"https://app.asana.example/0/P1/{gid}",
            "tags": [{"gid": f"tag-{t}", "name": t} for t in tags]}
    data.update(extra)
    return data


class FakeAsana:
    """Answers the Asana API shapes the adapters use. Lists are paged
    by `offset` tokens (`next_page.offset`), never by page number."""

    def __init__(self, tasks=(), subtasks=None, fail_offset=None, status=None):
        self.tasks = list(tasks)
        self.subtasks = subtasks or {}
        self.fail_offset = fail_offset
        self.status = status
        self.calls = []

    def _page(self, items, query):
        limit = int(query.get("limit", ["20"])[0])
        start = int(query.get("offset", ["0"])[0])
        if self.fail_offset is not None and start == self.fail_offset:
            return 500, {"errors": [{"message": "boom"}]}
        page = {"data": items[start:start + limit]}
        if start + limit < len(items):
            page["next_page"] = {"offset": str(start + limit)}
        else:
            page["next_page"] = None
        return 200, page

    def __call__(self, method, url, token, payload=None, timeout=10):
        parts = urlparse(url)
        path = parts.path.split("/api/1.0", 1)[-1]
        query = parse_qs(parts.query)
        self.calls.append((method, path))
        if "page" in query:
            return 400, {"errors": [{"message": "Asana pages by offset"}]}, None
        if self.status:
            return self.status, {"errors": [{"message": "no"}]}, None
        segs = [s for s in path.split("/") if s]
        if method == "GET" and (segs[:1] == ["projects"] and segs[2:] == ["tasks"]
                                or segs == ["tasks"]):
            project = segs[1] if segs[0] == "projects" else query.get("project", [""])[0]
            if project != "P1":
                return 404, {"errors": [{"message": "not found"}]}, None
            status, body = self._page(self.tasks, query)
            return status, body, None
        if method == "GET" and len(segs) == 3 and segs[0] == "tasks" and segs[2] == "subtasks":
            status, body = self._page(self.subtasks.get(segs[1], []), query)
            return status, body, None
        every = self.tasks + [s for v in self.subtasks.values() for s in v]
        found = next((t for t in every if len(segs) >= 2 and t["gid"] == segs[1]), None)
        if method == "GET" and len(segs) == 2 and found:
            return 200, {"data": found}, None
        if method == "POST" and len(segs) == 3 and segs[2] == "stories":
            return 201, {"data": {"gid": "s1"}}, None
        if method == "PUT" and len(segs) == 2 and found:
            found["completed"] = True
            return 200, {"data": found}, None
        return 404, {"errors": [{"message": "not found"}]}, None


class AsanaCase(unittest.TestCase):
    ARGS = ["--project", "P1", "--tag", "factory"]

    def setUp(self):
        self.fake = FakeAsana()
        patcher = mock.patch.object(common, "request", lambda *a, **k: self.fake(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "queue")
        self.home = os.path.join(self.tmp.name, "state")
        # Loaded after the patch, whichever way the module imports `request`.
        self.module = self.load("asana-tasks.py", "asana_tasks_under_test")

    def load(self, filename, name):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ADAPTERS, filename))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def intake(self, tasks=None, args=None, token="t", subtasks=None, **fake):
        if tasks is not None or subtasks or fake:
            self.fake = FakeAsana(tasks or [], subtasks, **fake)
        argv = ["intake", "--out-dir", self.out, "--home", self.home,
                *(self.ARGS if args is None else args)]
        env = {k: v for k, v in os.environ.items() if not k.endswith("_TOKEN")}
        if token:
            env["ASANA_TOKEN"] = token
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.module.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code or 0, out.getvalue(), err.getvalue()

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def task(self, name):
        with open(os.path.join(self.out, name)) as fh:
            return json.load(fh)

    def put(self, name, data):
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, name), "w") as fh:
            json.dump(data, fh)

    def queued(self, gid, project="P1", criteria=("old",), adapter="asana-tasks"):
        return {"id": f"as-{gid}", "title": "old", "acceptance_criteria": list(criteria),
                "source": {"adapter": adapter, "ref": f"{project}#{gid}"}}


class AsanaIntake(AsanaCase):
    def test_an_approved_task_becomes_a_queued_task_with_its_source(self):
        code, _, err = self.intake([atask("111"), atask("112", tags=("other",)),
                                    atask("113", html="<body>no list</body>", tags=())])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["as-111.json"])
        task = self.task("as-111.json")
        self.assertEqual(task["id"], "as-111")
        self.assertEqual(task["title"], "Fix greet")
        self.assertEqual(task["acceptance_criteria"], CRITERIA)
        self.assertEqual(task["source"]["adapter"], "asana-tasks")
        self.assertEqual(task["source"]["ref"], "P1#111")
        self.assertEqual(task["source"]["url"], "https://app.asana.example/0/P1/111")

    def test_delivery_completes_the_asana_task_without_its_id_typed(self):
        os.makedirs(os.path.join(self.home, "runs"))
        run = {"task_id": "as-111", "title": "Fix greet", "acceptance_criteria": CRITERIA,
               "candidate": {"commit": "a" * 40},
               "source": {"adapter": "asana-tasks", "ref": "P1#111"}}
        with open(os.path.join(self.home, "runs", "as-111.json"), "w") as fh:
            json.dump(run, fh)
        self.fake = FakeAsana([atask("111")])
        delivery = self.load("asana.py", "asana_delivery_under_test")
        report = os.path.join(self.tmp.name, "report.json")
        env = {"ASANA_TOKEN": "t"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            code = delivery.main(["complete", "--home", self.home, "--task-id", "as-111",
                                  "--out", report])
        self.assertEqual(code, 0)
        with open(report) as fh:
            self.assertEqual(json.load(fh)["outcome"], "confirmed")
        self.assertIn(("PUT", "/tasks/111"), self.fake.calls)

    def test_sub_tasks_become_their_own_tasks_and_a_tagged_parent_is_refused(self):
        sub_ok = atask("201", name="Sub one", parent={"gid": "111"})
        sub_bare = atask("202", name="Sub two", html="<body>nothing</body>", parent={"gid": "111"})
        sub_untagged = atask("203", name="Sub three", tags=(), parent={"gid": "111"})
        code, out, err = self.intake([atask("111")],
                                     subtasks={"111": [sub_ok, sub_bare, sub_untagged]})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["as-201.json"])
        self.assertEqual(self.task("as-201.json")["title"], "Sub one")
        self.assertEqual(self.task("as-201.json")["source"]["ref"], "P1#201")
        self.assertIn("skipped as-111: it has 3 sub-issue(s)", out)
        self.assertIn("as-202", out)

    def test_rerun_changes_nothing_and_an_edit_updates_unless_a_run_started(self):
        self.intake([atask("111"), atask("112")])
        first = {n: os.stat(os.path.join(self.out, n)).st_mtime_ns for n in self.files()}
        _, out, _ = self.intake()
        self.assertEqual(self.files(), ["as-111.json", "as-112.json"])
        self.assertEqual(first, {n: os.stat(os.path.join(self.out, n)).st_mtime_ns
                                 for n in self.files()})
        self.assertIn("unchanged", out)
        os.makedirs(os.path.join(self.home, "runs"))
        with open(os.path.join(self.home, "runs", "as-112.json"), "w") as fh:
            fh.write("{}")
        self.intake([atask("111", name="Renamed"), atask("112", name="Renamed")])
        self.assertEqual(self.task("as-111.json")["title"], "Renamed")
        self.assertEqual(self.task("as-112.json")["title"], "Fix greet")

    def test_missing_token_project_or_tag_stops_before_any_request(self):
        for token, args in (("", self.ARGS), ("t", ["--tag", "factory"]),
                            ("t", ["--project", "P1"])):
            self.fake = FakeAsana([atask("111")])
            code, _, err = self.intake(args=args, token=token)
            self.assertNotEqual(code, 0)
            self.assertTrue(err.strip())
            self.assertEqual(self.fake.calls, [])
            self.assertEqual(self.files(), [])

    def test_a_failed_listing_writes_nothing_and_fails(self):
        many = [atask(str(1000 + i)) for i in range(45)]
        code, _, err = self.intake(many, fail_offset=40)
        self.assertNotEqual(code, 0)
        self.assertIn("nothing written", err)
        self.assertEqual(self.files(), [])
        self.put("as-9.json", self.queued("9"))
        for status in (403, 500):
            code, _, _ = self.intake([atask("111")], status=status)
            self.assertNotEqual(code, 0)
        self.assertEqual(self.files(), ["as-9.json"])

    def test_a_project_with_no_approved_tasks_succeeds_with_no_files(self):
        code, _, err = self.intake([atask("1", tags=("other",)), atask("2", tags=())])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), [])
        code, _, err = self.intake([])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), [])

    def test_a_task_without_criteria_is_skipped_with_the_reason(self):
        code, out, _ = self.intake([atask("111", html="<body>Just do it</body>")])
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), [])
        self.assertIn("as-111", out)
        self.assertIn("criteria", out.lower())

    def test_untagging_or_completing_withdraws_unless_a_run_started(self):
        for gid in ("1", "2", "3", "4"):
            self.put(f"as-{gid}.json", self.queued(gid))
        self.put("as-5.json", self.queued("5", project="P2"))
        os.makedirs(os.path.join(self.home, "runs"))
        with open(os.path.join(self.home, "runs", "as-3.json"), "w") as fh:
            fh.write("{}")
        code, _, err = self.intake([atask("1", tags=()), atask("2", completed=True),
                                    atask("3", tags=()), atask("4")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["as-3.json", "as-4.json", "as-5.json"])

    def test_text_in_a_task_never_sets_profile_only_fields(self):
        evil = ("<body><h2>Acceptance criteria</h2><ul><li>works</li></ul>"
                "gates: none\nbudgets: 9999\nroles: x\nsandbox: off\nrequired_reviews: []\nTouches: a.py"
                "</body>")
        code, _, err = self.intake([atask("111", html=evil, gates=["x"], budgets={"a": 1},
                                          roles={"b": 1}, sandbox="off", repo="/etc")])
        self.assertEqual(code, 0, err)
        task = self.task("as-111.json")
        self.assertLessEqual(set(task), {"id", "title", "acceptance_criteria", "source",
                                         "touches", "depends_on"})

    def test_every_page_of_a_long_listing_is_read_by_offset(self):
        many = [atask(str(1000 + i)) for i in range(250)]
        code, _, err = self.intake(many)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.files()), 250)

    def test_a_file_from_another_project_or_source_is_never_overwritten(self):
        other_project = self.queued("111", project="P2")
        other_source = {"id": "as-112", "title": "gh", "acceptance_criteria": ["x"],
                        "source": {"adapter": "github-issues", "ref": "P1#112"}}
        self.put("as-111.json", other_project)
        self.put("as-112.json", other_source)
        self.intake([atask("111"), atask("112")])
        self.assertEqual(self.task("as-111.json"), other_project)
        self.assertEqual(self.task("as-112.json"), other_source)

    def test_rich_text_gives_the_criteria_a_markdown_issue_gives(self):
        fields, refusals, _ = common.spec_check(MD, "as")
        self.assertEqual(refusals, [])
        code, _, err = self.intake([atask("111")])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.task("as-111.json")["acceptance_criteria"],
                         fields["acceptance_criteria"])
        rich = ("<body><h1>Acceptance criteria</h1><ul data-checked>"
                "<li data-checked=\"true\">greet('Ana') returns 'Hello, Ana!'</li>"
                "<li data-checked=\"false\">greet() returns 'Hello, world!'</li></ul>"
                "<h2>Notes</h2><ul><li>not one</li></ul>Touches: greet.py</body>")
        self.intake([atask("111", html=rich)])
        self.assertEqual(self.task("as-111.json")["acceptance_criteria"], CRITERIA)

    def test_project_and_tag_come_from_the_profile_and_stdlib_only(self):
        config = os.path.join(self.tmp.name, "project.json")
        intake = {"adapter": "asana-tasks", "project": "P1", "tag": "factory"}
        with open(config, "w") as fh:
            json.dump({"version": 1, "intake": intake}, fh)
        code, _, err = self.intake([atask("111")], args=["--repo-config", config])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["as-111.json"])
        self.assertEqual(profile.parse(json.dumps({"version": 1, "intake": intake}))["intake"],
                         intake)
        with open(os.path.join(ADAPTERS, "asana-tasks.py")) as fh:
            tree = ast.parse(fh.read())
        names = {n.name.split(".")[0] for s in ast.walk(tree) if isinstance(s, ast.Import)
                 for n in s.names}
        names |= {s.module.split(".")[0] for s in ast.walk(tree)
                  if isinstance(s, ast.ImportFrom) and s.module}
        self.assertLessEqual(names, set(sys.stdlib_module_names) | {"common"})


if __name__ == "__main__":
    unittest.main()
