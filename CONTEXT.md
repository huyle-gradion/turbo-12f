# Context and truth boundaries

Vocabulary for turbo-12f. The CLI is the authority on what it
enforces; this file states what it does *not* claim.

- **Run**: one task, one run record (`runs/<id>.json`), one worktree,
  one append-only event log (`runs/<id>.events.jsonl`). Every command
  that changes a run holds `runs/<id>.lock` for its duration; a
  concurrent command on the same run is refused as busy. Different
  runs share nothing but the product repo's `.git` and run in
  parallel.
- **Evidence**: a stored artifact with path, SHA-256, revision,
  provenance. All evidence lives under the state home, never inside
  the product repo.
- **Candidate**: the commit `stage` pinned. A new commit is a new
  candidate; verification bound to the old one does not carry over.
- **Gate**: one ordered check from the task file. A later gate cannot
  run until the prior gate passed on the same candidate. Gates and
  replay refuse a dirty worktree or a moved HEAD, then run in a fresh
  checkout of the candidate commit (`verify/<id>`, removed after), so
  ignored or untracked files in the builder's worktree cannot affect
  them. Red does the same on the commit that adds the test. Each gate
  records the commit it ran on. Declared `verify_deps` are the one
  exception: linked in read-only, and named in every observation.
- **local-verified**: the configured evidence chain was observed and
  remains internally consistent. Not QC approval, not product
  acceptance, not permission to merge.
- **Worker**: an LLM invocation for one role on one task. The worker
  is replaceable; the policy/evidence engine is the durable part.
- **Host adapter**: code that calls a model, a tracker, or a forge.
  Adapters live in `adapters/` and report back through `t12 deliver`;
  the core never imports one.

## What this does not claim

- It is not a coding agent and not a sandbox. A worktree isolates
  files, not process privileges.
- Host declarations (model, sandbox, cost) are not verified facts.
  Spend is reported, estimated, or unknown.
- A same-user process can forge local files. This is a honesty and
  reproducibility layer, not a security boundary against a malicious
  local actor.
- Candidate binding holds against accidents (uncommitted, ignored,
  or later-written files in the builder's worktree). Against a
  builder that tampers on purpose it holds only when the builder ran
  inside `bin/t12-cage` and its edits reached the branch through
  `t12-cage commit`. The cage denies every write to the shared `.git`
  and the state home, so the builder cannot plant a hook, a
  `git replace` ref, a smudge filter, an `info/attributes` line, or a
  `.pth` file in the verify home. What stays open:
  - Nothing forces the cage. `t12` cannot tell a caged builder from
    an uncaged one, and the run record does not say which it was.
  - The worktree's `.git` pointer file is writable inside the cage.
    Only `t12-cage commit` checks it. `t12 stage` and the gates run
    git in the worktree without that check. Run `t12-cage commit`
    after `t12-cage` returns and before any `t12` command, and stop
    the run when it refuses.
  - `t12-cage` kills every process the builder started before it
    returns, including one that left its session with a double fork
    and `setsid`, so nothing caged is left to rewrite the pointer
    after the check. This fails when `t12-cage` itself dies from
    `SIGKILL`: the builder's processes then keep running in the cage.
    Do not commit a run whose `t12-cage` was killed that way.
  - On a kernel older than Landlock ABI 9 the builder can connect to
    a unix socket outside the cage (the session bus, a container
    daemon) and ask its owner to write for it. Older than ABI 6, it
    can also signal the operator's processes.
  - A path the operator adds with `--write` is outside the claim. A
    writable agent config directory lets the builder leave
    instructions for the next uncaged agent.
  - The cage limits writes to file contents and directory entries.
    It does not cover metadata: the builder can `chmod`, `utime`, or
    set an xattr on any file the operator owns, the shared `.git`
    included. That changes no content git reads, but it can make a
    file unreadable to the operator.
  - The cage does not stop data from leaving. It limits writes, not
    reads of what it is given, and Landlock restricts TCP only. UDP
    is open with or without `--network`, so the builder can send
    anything it can read to any host, for example in DNS queries.
- No network, no credentials, no model calls in the core or tests.
