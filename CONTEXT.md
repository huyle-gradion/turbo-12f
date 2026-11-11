# Context and truth boundaries

Vocabulary for turbo-12f. The CLI is the authority on what it
enforces; this file states what it does *not* claim.

- **Run**: one task, one run record (`runs/<id>.json`), one worktree,
  one append-only event log (`runs/<id>.events.jsonl`).
- **Evidence**: a stored artifact with path, SHA-256, revision,
  provenance. All evidence lives under the state home, never inside
  the product repo.
- **Candidate**: the commit `stage` pinned. A new commit is a new
  candidate; verification bound to the old one does not carry over.
- **Gate**: one ordered check from the task file. A later gate cannot
  run until the prior gate passed on the same candidate.
- **local-verified**: the configured evidence chain was observed and
  remains internally consistent. Not QC approval, not product
  acceptance, not permission to merge.
- **Worker**: an LLM invocation for one role on one task. The worker
  is replaceable; the policy/evidence engine is the durable part.
- **Host adapter**: code that calls a model, a tracker, or a forge.
  None exists in Phase 1. The core never imports one.

## What this does not claim

- It is not a coding agent and not a sandbox. A worktree isolates
  files, not process privileges.
- Host declarations (model, sandbox, cost) are not verified facts.
  Spend is reported, estimated, or unknown.
- A same-user process can forge local files. This is a honesty and
  reproducibility layer, not a security boundary against a malicious
  local actor.
- No network, no credentials, no model calls in the core or tests.
