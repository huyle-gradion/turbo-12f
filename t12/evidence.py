"""Evidence: write an artifact with its hash, verify it later by
re-reading and re-hashing. Certification re-verifies every ref;
a missing file, a changed byte, a symlink, or a path outside the
state home fails closed.
"""

import os

from . import model, store
from .errors import EvidenceError


def write_evidence(home, task_id, name, data: bytes):
    directory = model.evidence_dir(home, task_id)
    store.mkdir_private(directory)
    path = os.path.join(directory, name)
    store.atomic_write(path, data)
    return {
        "name": name,
        "path": path,
        "sha256": store.sha256_bytes(data),
        "bytes": len(data),
        "provenance": model.PROVENANCE,
    }


def write_observation(home, task_id, name, observation):
    """Store a command observation (argv, exit, duration, output)."""
    text = (
        f"$ {' '.join(observation['argv'])}\n"
        f"exit={observation['exit_code']} timed_out={observation['timed_out']} "
        f"duration_s={observation['duration_s']} sandbox={observation.get('sandbox', 'none')}\n"
        f"--- stdout ---\n{observation['stdout']}"
        f"--- stderr ---\n{observation['stderr']}"
    )
    ref = write_evidence(home, task_id, name, text.encode())
    ref["observation"] = {
        "argv": observation["argv"],
        "exit_code": observation["exit_code"],
        "timed_out": observation["timed_out"],
        "duration_s": observation["duration_s"],
        "sandbox": observation.get("sandbox", "none"),
    }
    return ref


def verify_ref(home, task_id, ref):
    if not ref or ref.get("provenance") != model.PROVENANCE:
        raise EvidenceError(f"evidence ref missing provenance: {ref and ref.get('name')}")
    path = store.ensure_inside(ref["path"], home)
    if not os.path.exists(path):
        raise EvidenceError(f"evidence missing: {ref['name']} ({path})")
    actual = store.sha256_file(path)
    if actual != ref["sha256"]:
        raise EvidenceError(
            f"evidence changed: {ref['name']} (recorded {ref['sha256'][:12]}, "
            f"on disk {actual[:12]})"
        )
    return True


def verify_green(home, run):
    """Re-validate the whole chain before certifying. Raises on any gap."""
    task_id = run["task_id"]
    if run["phase"] != "green":
        raise EvidenceError(f"green requires phase 'green', run is {run['phase']!r}")
    if not run["red"]:
        raise EvidenceError("green requires red evidence")
    if not run["replay"]:
        raise EvidenceError("green requires replay evidence")
    if not run["candidate"]:
        raise EvidenceError("green requires a staged candidate")
    for gate in run["gates"]:
        if gate["status"] != "passed" or not gate["observation"]:
            raise EvidenceError(f"gate {gate['name']!r} has not passed on this candidate")
        verify_ref(home, task_id, gate["observation"])
    verify_ref(home, task_id, run["red"]["output"])
    verify_ref(home, task_id, run["replay"]["output"])
    return True
