"""Property tests for the run state machine (Phase 2 hardening).

The transition matrix here is written out independently of
``t12.model.TRANSITIONS`` — it is the spec, not a copy of the code.
If the implementation ever allows a transition this matrix forbids,
or forbids one it allows, these tests fail.
"""

import copy
import json
import os
import tempfile
import time
import unittest

from t12 import model, profile, store
from t12.errors import BudgetError, StateError

# The spec: allowed targets per phase. A transition to the phase
# itself is always a no-op and is allowed on top of this matrix.
EXPECTED = {
    "unadmitted": {"ready"},
    "ready": {"red", "paused", "stopped"},
    "red": {"gates", "paused", "stopped"},
    "gates": {"gates", "green", "paused", "stopped"},
    # green -> gates is the review loop (Phase 6): a review can ask
    # for changes after replay, so the builder re-stages.
    "green": {"gates", "local-verified", "paused", "stopped"},
    "local-verified": set(),
    "paused": {"ready", "red", "gates", "green", "stopped"},
    "stopped": set(),
}


def _bare_run(phase):
    return {
        "version": model.VERSION,
        "task_id": "spec",
        "phase": phase,
        "prior_phase": None,
        "budgets": {
            "time_limit_s": 3600,
            "op_limit": 100,
            "repair_cap": 3,
            "started_at": time.time(),
            "ops_used": 0,
            "repairs_used": 0,
        },
    }


class TransitionMatrixTest(unittest.TestCase):
    def test_matrix_matches_spec(self):
        self.assertEqual(set(EXPECTED), set(model.PHASES))
        for phase in model.PHASES:
            for target in model.PHASES:
                run = _bare_run(phase)
                before = copy.deepcopy(run)
                allowed = target in EXPECTED[phase] or target == phase
                if allowed:
                    model.transition(run, target)
                    self.assertEqual(run["phase"], target, f"{phase} -> {target}")
                else:
                    with self.assertRaises(StateError, msg=f"{phase} -> {target}"):
                        model.transition(run, target)
                    # A refused transition must not mutate the run.
                    self.assertEqual(run, before, f"{phase} -> {target} mutated state")

    def test_every_phase_appears_in_transitions(self):
        self.assertEqual(set(model.TRANSITIONS), set(model.PHASES))
        for phase, targets in model.TRANSITIONS.items():
            self.assertEqual(targets, EXPECTED[phase], phase)


class BudgetTest(unittest.TestCase):
    def test_time_budget_exhausted(self):
        run = _bare_run("ready")
        run["budgets"]["started_at"] = time.time() - 4000
        with self.assertRaises(BudgetError):
            model.check_budgets(run)

    def test_op_budget_exhausted(self):
        run = _bare_run("ready")
        run["budgets"]["ops_used"] = 100
        with self.assertRaises(BudgetError):
            model.check_budgets(run)

    def test_repair_cap_exceeded(self):
        run = _bare_run("gates")
        run["budgets"]["repairs_used"] = 4
        with self.assertRaises(BudgetError):
            model.check_budgets(run)

    def test_budgets_ok_within_limits(self):
        run = _bare_run("ready")
        self.assertIn("wall_used_s", model.check_budgets(run))


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "runs"))
        self.run = _bare_run("ready")
        self.run["task_id"] = "chain-1"
        model.save_run(self.home, self.run)

    def tearDown(self):
        self.tmp.cleanup()

    def _events(self):
        return model.read_events(self.home, "chain-1")

    def _rewrite_events(self, events):
        path = model.events_path(self.home, "chain-1")
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True) + "\n")

    def test_chain_intact(self):
        model.append_event(self.home, self.run, "prepared", {"a": 1})
        model.append_event(self.home, self.run, "red", {"commit": "abc"})
        model.append_event(self.home, self.run, "staged", {})
        self.assertEqual(model.verify_chain(self.home, self.run), [])
        events = self._events()
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual(events[0]["prev"], model.genesis_prev("chain-1"))
        self.assertEqual(events[1]["prev"], events[0]["hash"])

    def test_chain_detects_edited_event(self):
        model.append_event(self.home, self.run, "prepared", {})
        model.append_event(self.home, self.run, "red", {})
        events = self._events()
        events[0]["detail"] = {"tampered": True}
        self._rewrite_events(events)
        problems = model.verify_chain(self.home, self.run)
        self.assertTrue(any("hash mismatch" in p for p in problems), problems)

    def test_chain_detects_deleted_event(self):
        model.append_event(self.home, self.run, "prepared", {})
        model.append_event(self.home, self.run, "red", {})
        model.append_event(self.home, self.run, "staged", {})
        events = self._events()
        self._rewrite_events([events[0], events[2]])
        problems = model.verify_chain(self.home, self.run)
        self.assertTrue(any("seq" in p or "prev link" in p for p in problems), problems)

    def test_chain_detects_edited_run_record(self):
        model.append_event(self.home, self.run, "prepared", {})
        self.run["budgets"]["op_limit"] = 999
        model.save_run(self.home, self.run)
        problems = model.verify_chain(self.home, self.run)
        self.assertTrue(any("state digest" in p for p in problems), problems)

    def test_chain_detects_broken_prev_link(self):
        model.append_event(self.home, self.run, "prepared", {})
        model.append_event(self.home, self.run, "red", {})
        events = self._events()
        events[1]["prev"] = "0" * 64
        self._rewrite_events(events)
        problems = model.verify_chain(self.home, self.run)
        self.assertTrue(any("prev link" in p for p in problems), problems)


class ArchiveTest(unittest.TestCase):
    def test_archive_preserves_run_and_events(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "runs"))
            run = _bare_run("stopped")
            run["task_id"] = "demo-1"
            model.save_run(home, run)
            model.append_event(home, run, "stopped", {"reason": "x"})
            base = model.archive_run(home, run)
            self.assertFalse(os.path.exists(model.run_path(home, "demo-1")))
            self.assertTrue(
                os.path.exists(os.path.join(home, "runs", f"{base}.json"))
            )
            self.assertTrue(
                os.path.exists(os.path.join(home, "runs", f"{base}.events.jsonl"))
            )
            archived = store.load_json(os.path.join(home, "runs", f"{base}.json"))
            self.assertEqual(archived["phase"], "stopped")



class ProfileTest(unittest.TestCase):
    GATE = {"name": "unit", "command": ["python3", "-m", "unittest"]}

    def profile(self, **fields):
        return profile.parse(json.dumps(dict({"version": 1}, **fields)))

    def test_editing_or_adding_a_profile_needs_a_security_review(self):
        for base in (profile.parse(None), self.profile(security_review_paths=["src/auth"])):
            roots = profile.security_roots(base)
            self.assertTrue(model.under(".t12/project.json", roots))
            self.assertFalse(model.under("src/other.py", roots))
        self.assertTrue(model.under("src/auth/login.py", roots))

    def test_a_malformed_profile_on_the_base_is_refused(self):
        for text in ("[]", "not json", '{"version": 2}', '{"version": 1, "gatez": []}',
                     '{"version": 1, "protected_paths": ["../x"]}',
                     '{"version": 1, "op_limit": true}'):
            with self.assertRaises(StateError, msg=text):
                profile.parse(text)

    def test_a_task_without_settings_takes_the_profile(self):
        merged = profile.apply(self.profile(gates=[self.GATE], op_limit=9), {"id": "t"})
        self.assertEqual(merged["gates"], [self.GATE])
        self.assertEqual(merged["op_limit"], 9)

    def test_a_task_can_tighten_but_never_loosen_the_profile(self):
        base = self.profile(gates=[self.GATE], required_reviews=["reviewer"],
                            sandbox_adapter="docker", require_probes=True)
        extra = {"name": "lint", "command": ["ruff", "check"]}
        merged = profile.apply(base, {"gates": [extra], "required_reviews": ["security"],
                                      "require_probes": False})
        self.assertEqual(merged["gates"], [self.GATE, extra])
        self.assertEqual(merged["required_reviews"], ["reviewer", "security"])
        self.assertTrue(merged["require_probes"])
        for loosen in ({"sandbox_adapter": "none"}, {"gates": [self.GATE]},
                       {"protected_paths": []}, {"security_review_paths": []}):
            with self.assertRaises(StateError, msg=loosen):
                profile.apply(base, loosen)

    def test_a_task_may_only_narrow_the_profiles_verify_deps(self):
        base = self.profile(verify_deps=[".venv", "node_modules"])
        self.assertEqual(profile.apply(base, {"verify_deps": [".venv"]})["verify_deps"], [".venv"])
        with self.assertRaises(StateError):
            profile.apply(base, {"verify_deps": ["vendor"]})

    def test_verify_deps_may_not_overlap_a_protected_path(self):
        base = self.profile(protected_paths=["guards"])
        for dep in ("guards/__pycache__", "guards"):
            with self.assertRaises(StateError, msg=dep):
                profile.apply(base, {"verify_deps": [dep]})

    def test_a_mistyped_task_value_is_a_clear_error(self):
        base = self.profile(gates=[self.GATE], required_reviews=["reviewer"])
        for bad in ({"gates": None}, {"required_reviews": None}, {"op_limit": "9"}):
            with self.assertRaises(StateError, msg=bad):
                profile.apply(base, bad)

    def test_the_task_sets_its_own_budget(self):
        merged = profile.apply(self.profile(op_limit=9), {"op_limit": 40})
        self.assertEqual(merged["op_limit"], 40)


if __name__ == "__main__":
    unittest.main()
