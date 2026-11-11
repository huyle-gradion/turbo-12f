"""Tests for limits (Phase 5): percentile math, calibration from
completed runs, censoring, limits.json round-trip, and reporting.
"""

import json
import os
import tempfile
import unittest

from t12 import limits, store
from t12.errors import StateError


def _record(task_id, phase, measurements=None, stop_reason=None, budgets=None):
    return {
        "task_id": task_id,
        "phase": phase,
        "stop_reason": stop_reason,
        "measurements": measurements,
        "budgets": budgets or {
            "time_limit_s": 3600, "op_limit": 100, "repair_cap": 3,
            "ops_used": 0, "repairs_used": 0,
        },
        "budget_source": "builtin",
    }


def _measurements(wall, ops, repairs):
    return {"wall_used_s": wall, "ops_used": ops, "repairs_used": repairs,
            "gates": 1, "finished_at": 0.0}


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(limits.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertEqual(limits.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9)
        self.assertEqual(limits.percentile([42], 90), 42)
        self.assertEqual(limits.percentile([3, 1, 2], 50), 2)

    def test_round_up(self):
        self.assertEqual(limits.round_up(61, 60), 120)
        self.assertEqual(limits.round_up(60, 60), 60)
        self.assertEqual(limits.round_up(0, 5), 0)
        self.assertEqual(limits.round_up(7, 5), 10)

    def test_summarize(self):
        self.assertIsNone(limits.summarize([]))
        summary = limits.summarize([10, 20, 30])
        self.assertEqual((summary["n"], summary["p50"], summary["p90"], summary["max"]),
                         (3, 20, 30, 30))


class RecommendTest(unittest.TestCase):
    def _completed(self, triples):
        return [{"measurements": _measurements(w, o, r)} for w, o, r in triples]

    def test_too_few_runs(self):
        self.assertIsNone(limits.recommend(self._completed([(10, 5, 0), (20, 6, 0)])))
        self.assertIsNone(limits.recommend([]))

    def test_floors_apply_to_toy_runs(self):
        rec = limits.recommend(self._completed([(10, 5, 0), (20, 6, 0), (30, 7, 1)]))
        self.assertEqual(rec["sample"], 3)
        self.assertEqual(rec["recommended"],
                         {"time_limit_s": 600, "op_limit": 20, "repair_cap": 2})

    def test_p90_drives_recommendation(self):
        rec = limits.recommend(self._completed([
            (900, 40, 1), (1200, 50, 2), (1500, 60, 2), (1800, 70, 3), (2000, 80, 3),
        ]))
        self.assertEqual(rec["recommended"]["time_limit_s"], 2040)
        self.assertEqual(rec["recommended"]["op_limit"], 80)
        self.assertEqual(rec["recommended"]["repair_cap"], 3)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "runs"))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, record):
        path = os.path.join(self.home, "runs", name)
        with open(path, "w") as fh:
            json.dump(record, fh)

    def test_collect_splits_completed_censored_superseded(self):
        for i in range(3):
            self._write(f"v{i}.json", _record(
                f"v{i}", "local-verified", _measurements(10 + i, 5 + i, 0)))
        self._write("capped.json", _record(
            "capped", "stopped", _measurements(300, 50, 4), stop_reason="repair-cap"))
        self._write("abandoned.json", _record("abandoned", "stopped", None,
                                              stop_reason="bad approach"))
        self._write("old.superseded-123.json", _record(
            "old", "local-verified", _measurements(5, 5, 0)))
        with open(os.path.join(self.home, "runs", "broken.json"), "w") as fh:
            fh.write("{not json")
        data = limits.collect(self.home)
        self.assertEqual(len(data["completed"]), 3)
        self.assertEqual(len(data["censored"]), 1)
        self.assertEqual(data["censored"][0]["task_id"], "capped")
        self.assertEqual(len([r for r in data["runs"] if r["superseded"]]), 1)
        self.assertEqual(data["unreadable"], ["broken.json"])
        self.assertEqual(data["cap_hits"]["repair"], 1)
        rec = limits.recommend(data["completed"])
        self.assertEqual(rec["sample"], 3)

    def test_empty_home(self):
        data = limits.collect(self.home)
        self.assertEqual(data["runs"], [])
        self.assertIsNone(limits.recommend(data["completed"]))


class LimitsFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_load_round_trip(self):
        rec = {"sample": 3,
               "stats": {"time_limit_s": {"n": 3, "p50": 20, "p90": 30, "max": 30}},
               "recommended": {"time_limit_s": 600, "op_limit": 20, "repair_cap": 2}}
        limits.save_limits(self.home, rec)
        loaded = limits.load_limits(self.home)
        self.assertEqual(loaded, {"time_limit_s": 600, "op_limit": 20, "repair_cap": 2})
        self.assertEqual(store.load_json(limits.limits_path(self.home))["calibrated_from"]["runs"], 3)

    def test_no_file_is_none(self):
        self.assertIsNone(limits.load_limits(self.home))

    def test_partial_keys_fill_from_builtins(self):
        store.save_json(limits.limits_path(self.home), {"op_limit": 40})
        loaded = limits.load_limits(self.home)
        self.assertEqual(loaded["op_limit"], 40)
        self.assertEqual(loaded["time_limit_s"], 3600)

    def test_malformed_file_fails_closed(self):
        with open(limits.limits_path(self.home), "w") as fh:
            fh.write("{oops")
        with self.assertRaises(StateError):
            limits.load_limits(self.home)
        store.save_json(limits.limits_path(self.home), {"op_limit": 0})
        with self.assertRaises(StateError):
            limits.load_limits(self.home)


if __name__ == "__main__":
    unittest.main()
