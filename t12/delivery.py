"""Delivery (Phase 7): recording what happened outside the factory.

The core never talks to a forge or Asana and holds no credentials.
An adapter — a separate program the host runs — does the remote
work and writes a JSON report. `t12 deliver` records that report
as evidence. The core's job is to make the record honest:

- Delivery only happens for a `local-verified` run, with the event
  chain intact and HEAD on the candidate.
- The delivery is pinned to the reviewed head SHA: the report's
  head must equal the candidate commit. A delivery for any other
  commit is refused.
- Outcomes are exactly four, and they mean what they say:
    confirmed — the remote state was READ BACK and matches
        (the PR exists with the reviewed head, the Asana task is
        complete). Delivery is done.
    rejected — the remote definitively said no, or the remote state
        definitively does not match (the PR's head is a different
        commit, the task does not exist). A retry may make sense
        after a fix, but nothing was delivered.
    denied — the mutation was refused for permission/policy reasons
        (403 on the create/complete call). Nothing was delivered.
    uncertain — the adapter could not establish what happened
        (timeout, 5xx, a read that failed). Nothing may be assumed.
- An uncertain outcome blocks further delivery for that adapter
  until it is resolved. The block exists because a blind retry
  after an uncertain mutation can duplicate the effect (a second
  PR, a second completion comment) — or miss that the first one
  landed. Resolution is an explicit `--resolves` delivery whose
  report describes the read-back, not a new action.
- Two mislabelings from the original Turbo's integration work are
  ruled out here: a 401/403 on a READ is uncertain (the adapter
  could not establish state), never denied — denied means a
  mutation was refused. And a 5xx during confirmation is
  uncertain, never rejected — rejected means the remote
  definitively said no. The adapters enforce this; the core
  records whatever outcome the report claims, so the report is
  where the distinction is made and audited.
"""

import time

from .errors import StateError

ADAPTERS = ("github", "gitlab", "bitbucket", "asana")
OUTCOMES = ("confirmed", "rejected", "denied", "uncertain")


def last_delivery(run, adapter):
    for delivery in reversed(run.get("deliveries") or []):
        if delivery["adapter"] == adapter:
            return delivery
    return None


def check_allowed(run, adapter, resolves):
    """Delivery needs a certified run and a clear lane. An open
    uncertain outcome blocks a new delivery attempt for that
    adapter until an explicit resolving report arrives."""
    if run["phase"] != "local-verified":
        raise StateError(
            f"deliver requires phase 'local-verified', run is {run['phase']!r}"
        )
    last = last_delivery(run, adapter)
    if last is not None and last["outcome"] == "uncertain" and not resolves:
        raise StateError(
            f"delivery via {adapter} is blocked: the last outcome was "
            f"uncertain ({last.get('detail', '')}). Re-run the adapter "
            f"as a read-back and record it with --resolves; never retry "
            f"the action blind."
        )


def check_head(run, head):
    candidate = (run["candidate"] or {}).get("commit")
    if not candidate:
        raise StateError("deliver requires a staged candidate")
    if head != candidate:
        raise StateError(
            f"deliver is pinned to the reviewed head {candidate[:12]}; "
            f"the report claims {str(head)[:12]}"
        )


def record_delivery(run, adapter, report, report_ref):
    """Append a delivery attempt. The report is the adapter's claim;
    the core checks its shape and its head pinning, then records it
    with the evidence ref of the stored report."""
    outcome = report.get("outcome")
    if outcome not in OUTCOMES:
        raise StateError(f"delivery report has an unknown outcome: {outcome!r}")
    check_head(run, report.get("head"))
    deliveries = run.setdefault("deliveries", [])
    n = len([d for d in deliveries if d["adapter"] == adapter]) + 1
    entry = {
        "adapter": adapter,
        "outcome": outcome,
        "head": report.get("head"),
        "ref": report.get("ref"),
        "detail": report.get("detail", ""),
        "report": report_ref,
        "at": time.time(),
        "n": n,
    }
    deliveries.append(entry)
    return entry
