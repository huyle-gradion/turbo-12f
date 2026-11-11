"""Limits (Phase 5): budget reporting and calibration from real runs.

Defaults are guesses until measured. This module collects what runs
actually used (wall time, operations, repairs) from the run records,
summarizes the distribution over completed runs, and recommends
defaults from the 90th percentile — with floors, so a handful of
toy runs cannot calibrate the factory down to nothing, and a
minimum sample, so one run cannot set the defaults. A task's own
budgets always win over calibrated defaults; calibrated defaults
win over the built-ins.
"""

import glob
import math
import os
import time

from . import store
from .errors import StateError

BUILTIN_DEFAULTS = {"time_limit_s": 3600, "op_limit": 100, "repair_cap": 3}
FLOORS = {"time_limit_s": 600, "op_limit": 20, "repair_cap": 2}
STEPS = {"time_limit_s": 60, "op_limit": 5, "repair_cap": 1}
MIN_SAMPLE = 3
KEYS = ("time_limit_s", "op_limit", "repair_cap")
MEASURED = {"time_limit_s": "wall_used_s", "op_limit": "ops_used", "repair_cap": "repairs_used"}


def percentile(values, pct):
    """Nearest-rank percentile of a non-empty list of numbers."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def round_up(value, step):
    return int(math.ceil(value / step) * step)


def summarize(values):
    if not values:
        return None
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "max": max(values),
    }


def limits_path(home):
    return os.path.join(home, "limits.json")


def load_limits(home):
    """Calibrated defaults for this state home, or None. A malformed
    file fails closed — a silently ignored limits file would be a
    lie about what the defaults are."""
    path = limits_path(home)
    if not os.path.exists(path):
        return None
    try:
        data = store.load_json(path)
    except (OSError, ValueError) as exc:
        raise StateError(f"limits file {path} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"limits file {path} is not an object")
    limits = dict(BUILTIN_DEFAULTS)
    for key in KEYS:
        if key in data:
            try:
                value = int(data[key])
            except (TypeError, ValueError) as exc:
                raise StateError(f"limits file {path}: {key!r} is not an integer") from exc
            if value <= 0:
                raise StateError(f"limits file {path}: {key!r} must be > 0")
            limits[key] = value
    return limits


def save_limits(home, recommendation):
    data = {
        "time_limit_s": recommendation["recommended"]["time_limit_s"],
        "op_limit": recommendation["recommended"]["op_limit"],
        "repair_cap": recommendation["recommended"]["repair_cap"],
        "calibrated_from": {
            "runs": recommendation["sample"],
            "at": time.time(),
            "stats": recommendation["stats"],
        },
    }
    store.save_json(limits_path(home), data)
    return data


def collect(home):
    """Read every run record in the state home and split it into
    what can calibrate defaults and what cannot. Completed runs
    (local-verified with measurements) calibrate; runs stopped by
    the repair cap are censored — their true need is unknown — and
    are reported as such, not folded into the numbers."""
    runs_dir = os.path.join(home, "runs")
    runs, unreadable = [], []
    if os.path.isdir(runs_dir):
        for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
            try:
                record = store.load_json(path)
            except (OSError, ValueError):
                unreadable.append(os.path.basename(path))
                continue
            runs.append({
                "task_id": record.get("task_id", "?"),
                "phase": record.get("phase"),
                "stop_reason": record.get("stop_reason"),
                "superseded": ".superseded-" in os.path.basename(path),
                "measurements": record.get("measurements"),
                "budgets": record.get("budgets", {}),
                "budget_source": record.get("budget_source"),
            })
    completed = [r for r in runs
                 if r["phase"] == "local-verified" and r["measurements"]
                 and not r["superseded"]]
    censored = [r for r in runs
                if r["stop_reason"] == "repair-cap" and not r["superseded"]]
    return {
        "runs": runs,
        "unreadable": unreadable,
        "completed": completed,
        "censored": censored,
        "cap_hits": {
            "repair": len(censored),
            "ops": len([r for r in runs
                        if r["budgets"].get("ops_used", 0) >= r["budgets"].get("op_limit", 0)
                        and r["budgets"].get("op_limit")]),
            "time": len([r for r in runs
                         if r["measurements"]
                         and r["measurements"].get("wall_used_s", 0) >= r["budgets"].get("time_limit_s", 0)
                         and r["budgets"].get("time_limit_s")]),
        },
    }


def recommend(completed):
    """Recommended defaults from completed runs, or None when the
    sample is too small to say anything. The recommendation is the
    p90 rounded up to a sane step, never below the floor."""
    if len(completed) < MIN_SAMPLE:
        return None
    stats = {}
    recommended = {}
    for key in KEYS:
        measured_key = MEASURED[key]
        values = [r["measurements"][measured_key] for r in completed
                  if isinstance(r["measurements"].get(measured_key), (int, float))]
        summary = summarize(values)
        stats[key] = summary
        recommended[key] = max(FLOORS[key], round_up(summary["p90"], STEPS[key]))
    return {"sample": len(completed), "stats": stats, "recommended": recommended}


def report_text(home, data, recommendation):
    lines = [f"budget report — home: {home}"]
    runs = data["runs"]
    verified = len([r for r in runs if r["phase"] == "local-verified"])
    stopped = len([r for r in runs if r["phase"] == "stopped"])
    superseded = len([r for r in runs if r["superseded"]])
    lines.append(
        f"runs: {len(runs)} total "
        f"({verified} verified, {stopped} stopped, {superseded} superseded)"
    )
    if data["unreadable"]:
        lines.append(f"unreadable run records: {', '.join(data['unreadable'])}")
    for entry in runs:
        budgets = entry["budgets"]
        m = entry["measurements"]
        if m:
            wall = m.get("wall_used_s", 0)
            lines.append(
                f"  {entry['task_id']}: {entry['phase']} "
                f"wall {wall:.1f}s/{budgets.get('time_limit_s')}s "
                f"ops {m.get('ops_used')}/{budgets.get('op_limit')} "
                f"repairs {m.get('repairs_used')}/{budgets.get('repair_cap')}"
                + (" (superseded)" if entry["superseded"] else "")
            )
        else:
            lines.append(
                f"  {entry['task_id']}: {entry['phase']} (no measurements)"
                + (" (superseded)" if entry["superseded"] else "")
            )
    hits = data["cap_hits"]
    lines.append(
        f"cap hits: repairs {hits['repair']}, ops {hits['ops']}, time {hits['time']}"
    )
    if data["censored"]:
        lines.append(
            f"censored from calibration (stopped by repair cap): "
            f"{', '.join(r['task_id'] for r in data['censored'])}"
        )
    if recommendation is None:
        lines.append(
            f"calibration: not enough data "
            f"({len(data['completed'])} completed runs, need at least {MIN_SAMPLE})"
        )
    else:
        rec = recommendation["recommended"]
        stats = recommendation["stats"]
        lines.append(
            f"calibration: from {recommendation['sample']} completed runs — "
            f"wall p50 {stats['time_limit_s']['p50']:.1f}s "
            f"p90 {stats['time_limit_s']['p90']:.1f}s "
            f"max {stats['time_limit_s']['max']:.1f}s; "
            f"ops p50 {stats['op_limit']['p50']} "
            f"p90 {stats['op_limit']['p90']} "
            f"max {stats['op_limit']['max']}; "
            f"repairs p50 {stats['repair_cap']['p50']} "
            f"p90 {stats['repair_cap']['p90']} "
            f"max {stats['repair_cap']['max']}"
        )
        lines.append(
            f"recommended defaults: time_limit_s={rec['time_limit_s']} "
            f"op_limit={rec['op_limit']} repair_cap={rec['repair_cap']}"
        )
    return "\n".join(lines) + "\n"
