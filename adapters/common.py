"""Shared helpers for the delivery adapters (Phase 7).

Adapters are separate programs: they run outside the factory core,
hold the host's credentials, and talk to the network. The core never
imports this module. Adapters are stdlib-only.

Outcome rules (from the original Turbo's integration lessons):
- A failed READ is always `uncertain`. The adapter could not
  establish the remote state — a 401/403 or a 5xx on a read is
  not a denial and not a rejection.
- A failed MUTATION is `denied` on 403 (permission/policy),
  `rejected` on 404/422 and other definite 4xx (the remote said
  no), and `uncertain` on 5xx, timeouts, and transport errors
  (the mutation may or may not have landed).
- An adapter exits 0 when it wrote a report, whatever the outcome.
  A non-zero exit means the adapter itself could not run (missing
  token, missing run record, bad arguments) and no report exists.
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def read_run(home, task_id):
    path = os.path.join(home, "runs", f"{task_id}.json")
    if not os.path.isfile(path):
        raise SystemExit(f"error: no run record for {task_id} in {home}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def manifest_text(home, task_id):
    path = os.path.join(home, "evidence", task_id, "manifest.txt")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def change_request_body(home, task_id, run, candidate):
    manifest = manifest_text(home, task_id)
    return (
        f"{run['title']}\n\n"
        f"Candidate: {candidate}\n"
        f"Acceptance criteria: {'; '.join(run.get('acceptance_criteria') or [])}\n"
        + (f"\n{manifest}" if manifest else "")
    )


def origin_url(run):
    try:
        return subprocess.run(
            ["git", "-C", run["repo"], "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def parse_remote(url):
    """(host, path) from an scp-style or URL git remote, or None.
    The path has no leading slash and no .git suffix."""
    url = url or ""
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$", url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https", "ssh", "git") or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return (host.lower(), path) if path else None


def request(method, url, token, payload=None, timeout=10):
    """One HTTP call. Returns (status, body, error):
    status is None on a transport failure (timeout, DNS, refused);
    body is parsed JSON when the response has any; error describes a
    transport failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _parse_json(raw), None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, _parse_json(raw), None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"transport: {exc}"


def _parse_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


def read_outcome(status, error):
    """Outcome for a failed read: always uncertain. The remote state
    is unknown, so nothing may be concluded from the failure."""
    if error is not None:
        return "uncertain", error
    if status in (401, 403):
        return "uncertain", f"read returned {status} (could not establish remote state)"
    if status is not None and status >= 500:
        return "uncertain", f"read returned {status}"
    return "uncertain", f"read returned {status}"


def mutation_outcome(status, error, body=None):
    """Outcome for a failed mutation: denied only when the remote
    refused permission; rejected when it definitively said no;
    uncertain when the effect is unknown."""
    if error is not None:
        return "uncertain", error
    if status == 403:
        return "denied", _message(body, f"mutation forbidden ({status})")
    if status == 404:
        return "rejected", _message(body, f"mutation target not found ({status})")
    if status == 422:
        return "rejected", _message(body, f"mutation rejected ({status})")
    if status is not None and status >= 500:
        return "uncertain", f"mutation returned {status} (effect unknown)"
    if status is not None and 400 <= status < 500:
        return "rejected", _message(body, f"mutation rejected ({status})")
    return "uncertain", f"mutation returned {status}"


def _message(body, default):
    if isinstance(body, dict):
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("message"):
            return str(nested["message"])[:300]
        for key in ("message", "errors"):
            value = body.get(key)
            if value:
                return str(value)[:300]
    return default


def report(adapter, task_id, head, outcome, ref, detail):
    return {
        "adapter": adapter,
        "task_id": task_id,
        "head": head,
        "outcome": outcome,
        "ref": ref,
        "detail": detail,
        "checked_at": time.time(),
    }


def write_report(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path
