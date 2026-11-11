"""Property tests for the run state machine (Phase 2 hardening).

The transition matrix here is written out independently of
``t12.model.TRANSITIONS`` — it is the spec, not a copy of the code.
If the implementation ever allows a transition this matrix forbids,
or forbids one it allows, these tests fail.
"""

import copy
import os
import tempfile
import time
import unittest

from t12 import model, store
from t12.errors import BudgetError, StateError

# The spec: allowed targets per phase. A transition to the phase
# itself is always a no-op and is allowed on top of this matrix.
EXPECTED = {
    "unadmitted": {"ready"},
    "ready": {"red", "paused", "stopped"},
    "red": {"gates", "paused", "stopped"},
    "gates": {"gates", "green", "paused", "stopped"},
    "green": {"local-verified", "paused", "stopped"},
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


if __name__ == "__main__":
    unittest.main()
