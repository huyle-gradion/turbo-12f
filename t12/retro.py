"""Retrospective across runs: the lessons that keep recurring, each with
a proposal for the check that would catch it. Read-only. Like
`calibrate`, it measures and proposes; a human decides what changes.
"""

import collections
import glob
import json
import os
import statistics

from . import model, store

MIN_COUNT = 3


def _events(path):
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _review_findings(home, event):
    """Finding classes from a structured review a worker submitted
    through `act`, or None when its stored output no longer matches
    the recorded hash. It counts only if the next event records the
    review; `act` logs the output before the review step can refuse."""
    ref = event["detail"]["output"]
    path = os.path.join(model.evidence_dir(home, event["task_id"]), ref["name"])
    try:
        if store.sha256_file(path) != ref["sha256"]:
            return None
        output = store.load_json(path)
    except (OSError, ValueError):
        return None
    return [f["class"] if isinstance(f.get("class"), str) else "unclassified"
            for f in output["args"]["findings"] if isinstance(f, dict)]


def collect(home):
    gates = collections.defaultdict(lambda: {"runs": set(), "failed": set(),
                                             "failures": 0, "durations_s": []})
    stops = collections.Counter()
    rejected = collections.Counter()
    findings = collections.Counter()
    escalations = collections.defaultdict(set)
    reviews = unreadable = runs = 0
    reviewed_runs = set()
    for log in sorted(glob.glob(os.path.join(home, "runs", "*.events.jsonl"))):
        runs += 1
        run_name = os.path.basename(log)[:-len(".events.jsonl")]
        classes_seen = set()
        submitted = None
        for event in _events(log):
            kind, detail = event.get("type"), event.get("detail") or {}
            if kind == "reviewed" and submitted is not None:
                reviews += 1
                reviewed_runs.add(run_name)
                classes_seen.update(submitted)
            submitted = None
            if kind in ("gate-passed", "gate-failed"):
                gate = gates[detail.get("gate") or detail.get("step")]
                gate["runs"].add(run_name)
                if "duration_s" in detail:
                    gate["durations_s"].append(detail["duration_s"])
                if kind == "gate-failed":
                    gate["failed"].add(run_name)
                    gate["failures"] += 1
            elif kind == "stopped":
                stops[detail.get("reason")] += 1
            elif kind == "worker-output-rejected":
                rejected[(detail.get("role"), detail.get("error"))] += 1
            elif kind == "human-contact":
                escalations[detail.get("question", "").strip()].add(run_name)
            elif kind == "worker-output" and detail.get("tool") == "review":
                submitted = _review_findings(home, event)
                if submitted is None:
                    unreadable += 1
        findings.update(classes_seen)
    return {
        "runs": runs,
        "gates": {name: {"runs": len(g["runs"]), "failed_runs": len(g["failed"]),
                         "failures": g["failures"], "durations_s": g["durations_s"]}
                  for name, g in sorted(gates.items())},
        "stops": dict(stops),
        "rejected": [{"role": role, "error": error, "count": n}
                     for (role, error), n in rejected.most_common()],
        "reviews": reviews,
        "unreadable_reviews": unreadable,
        "reviewed_runs": len(reviewed_runs),
        "runs_with_finding": dict(findings.most_common()),
        "escalations": [{"question": q, "runs": sorted(r)}
                        for q, r in sorted(escalations.items(), key=lambda kv: -len(kv[1]))],
    }


def proposals(data, min_count=MIN_COUNT):
    out = []
    for name, gate in data["gates"].items():
        if gate["failed_runs"] >= min_count:
            out.append({
                "kind": "gate",
                "evidence": f"gate {name} failed in {gate['failed_runs']} of {gate['runs']} runs "
                            f"({gate['failures']} failures)",
                "proposal": f"find what the builder keeps missing and catch it before {name}: "
                            "a refusal in `stage`, or a cheaper gate ordered first",
            })
    for item in data["rejected"]:
        if item["count"] >= min_count:
            out.append({
                "kind": "rejected-output",
                "evidence": f"{item['role']} output rejected {item['count']} times: {item['error']}",
                "proposal": f"make the error in tools.py name the fix, and show the field "
                            f"in the {item['role']} prompt's example",
            })
    for cls, n in data["runs_with_finding"].items():
        if n >= min_count:
            out.append({
                "kind": "finding",
                "evidence": f"{cls} in {n} of {data['reviewed_runs']} reviewed runs",
                "proposal": "add a gate, probe, or state-machine refusal that catches this "
                            "before review",
            })
    for item in data["escalations"]:
        if len(item["runs"]) >= min_count:
            out.append({
                "kind": "escalation",
                "evidence": f"asked in {len(item['runs'])} runs ({', '.join(item['runs'])}): "
                            f"{item['question']}",
                "proposal": "put the answer in the task template or the acceptance criteria",
            })
    return out


def report_text(home, data, found):
    lines = [f"turbo-12f retro — {home}", f"runs read: {data['runs']}", "", "Gates:"]
    for name, gate in data["gates"].items():
        durations = gate["durations_s"]
        timing = (f"  median {statistics.median(durations):.1f}s, max {max(durations):.1f}s"
                  if durations else "")
        lines.append(f"  {name}: failed in {gate['failed_runs']} of {gate['runs']} runs{timing}")
    lines.append("")
    lines.append("Stops: " + (", ".join(f"{k} {v}" for k, v in data["stops"].items()) or "none"))
    lines.append(f"Structured reviews read: {data['reviews']}"
                 + (f" ({data['unreadable_reviews']} unreadable)" if data["unreadable_reviews"] else ""))
    lines.append("")
    lines.append("Proposals:" if found else "Proposals: none above the threshold")
    for p in found:
        lines.append(f"  [{p['kind']}] {p['evidence']}")
        lines.append(f"      -> {p['proposal']}")
    lines.append("")
    lines.append("retro changes nothing; turn a proposal into a task to act on it.")
    return "\n".join(lines) + "\n"
