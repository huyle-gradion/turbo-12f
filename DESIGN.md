# turbo-12f — design

Working name. Local-only rebuild of Turbo's best ideas, strictly on
Dex Horthy's 12-Factor Agents (HumanLayer), adopting the super-board
reviewer workflow. Not the GitHub `huyle-gradion/turbo` repo; nothing
here talks to GitHub or Asana yet. Python standard library only.

## What we kept from Turbo (Michael's mapping, treated as opinion)

- **Evidence, not declarations.** A pass is a stored artifact: exact
  argv, exit code, duration, raw output, SHA-256, git revision, test
  blob hash, config digest, provenance `observed-helper-execution`.
  `green` re-reads and re-hashes everything before certifying.
- **Fail closed.** Missing/stale/changed evidence, a moved HEAD, a
  changed test after red, a reused approval on a new candidate:
  stop and explain, never "try your best".
- **Exact revision binding.** Every result names candidate commit,
  base commit, test blob, gate-config digest. A new commit is a new
  candidate; old verification does not carry over.
- **Bounded autonomy.** One task, time limit, operation limit, repair
  cap, explicit scope, disposable worktree, no direct merge to `main`.
- **Separate roles.** Builder is never the only judge. Test-writer and
  implementer are different sessions of the same model; reviewer
  forms its own hypotheses before reading the builder's summary.
- **Human authority explicit.** `local-verified` means "the configured
  evidence chain was observed and is internally consistent". It is not
  acceptance, not permission to merge, not a safety claim.
- **Deterministic core, replaceable host.** The core owns state,
  validation, execution, evidence, hashes, git invariants, timeouts,
  budgets. Calling a model, opening a PR, talking to a tracker are
  host adapters, added later, never inside the core.
- **The state machine.** `unadmitted -> ready -> red -> gates ->
  green -> local-verified`, plus `paused` and `stopped`.
- **Build order.** Minimal evidence runner first; state machine;
  safe execution; evidence integrity; limits; roles; delivery last.

## What we deliberately dropped / changed vs Turbo

- No Asana, GitHub, Antigravity/`agy`, CI, or delivery code in
  Phase 1. Those are host adapters (Phase 7), not the core.
- No framework, no agent loop that lets the LLM pick the next step.
  The LLM never decides the phase. Control flow is code (Factor 8).
- No thermos-style re-review loop, no deferred-review backlog. A
  review finding either blocks (bounce to owner lane) or is filed as
  a future ticket; it never silently re-queues.
- Test-only-what-changed is a policy the gates encode, not a hope:
  gates are explicit commands in the task file, and `green` refuses
  to certify on anything but the recorded evidence.
- The worker is one narrow job, not "the factory". The factory is
  the deterministic shell around it.

## The bottleneck this version is designed against

Your diagnosis from Turbo (2026-11-07), stated again for this
rebuild: delivery already takes mostly a day and still cannot ship
even two issues, and the bottleneck is testing and CI — the time
spent testing, retesting, checking, and verifying. Three mechanisms
make it worse, and this version is built to break all three:

1. **TDD cost per issue.** Every issue pays red/green iterations,
   targeted tests, a full suite on the final candidate, a post-merge
   suite, and CI. On Turbo the post-merge suite alone reached 426
   tests with a ~1,197s (~20 min) wall on main @ d201c7b — and that
   was the backstop, not the per-iteration cost. Re-running the full
   suite on every iteration is what made each issue take a day.
   Countermeasure here: test-only-what-changed is encoded in the
   gates. Gates are explicit per-task commands; the full suite runs
   only if the task declares it as a gate. `green` certifies only
   the recorded evidence, never a fresh full run.
2. **Tests with no value.** A test written by the agent that
   implements the code proves nothing: it is written to pass with
   the code, so green is the same claim twice — the test and the
   code agree with each other, and neither is checked against the
   acceptance criteria. Countermeasure here: the test-writer and
   the implementer are different sessions; the implementer must not
   write or edit tests (`prompts/test-writer.md`,
   `prompts/builder.md`, `skills/orchestrator/SKILL.md`). And a test
   only counts if the red evidence shows it failing before the
   implementation and the replay shows it passing after — same
   command, same test blob, both recorded.
3. **Useless tests accumulate.** Every issue adds tests and almost
   none are ever removed: 283 tests after issues #20–#22, 409 at
   the consolidation baseline, 426 by d201c7b. Each new test slows
   every future run, so delivery gets worse the more you ship —
   consolidation issue #91 needed 18+ net removals just to get back
   under 409. Countermeasure here: consolidation is a standing rule
   in the test-writer prompt (combine close cases into one, remove
   unnecessary ones, simplify), and the reviewer treats a test that
   merely restates the implementation as a Verification miss
   (`skills/reviewer/SKILL.md`).

**Still open, not yet built:** there is no hard test budget. Nothing
caps new tests per task or suite time per run. The next decision is
whether to add one to the task file (e.g. `max_new_tests`, or a
suite-time ceiling enforced as a gate) or to rely on the reviewer +
consolidation rule above.

## The 12 factors — how each one is implemented here

| # | Factor | Implementation in turbo-12f |
|---|---|---|
| 1 | Natural language to tool calls | The LLM turns an owned prompt + context into file edits in the worktree or one CLI step (`red`, `stage`, `review`, `escalate`, ...). It never merges and never decides the phase: the CLI refuses any step the state machine does not allow. `prompts/*.md`, `t12/model.py` (transitions). **Not yet built:** no code parses or validates a worker's own output; the worker or orchestrator acts only by running the CLI. |
| 2 | Own your prompts | Prompts are versioned files under `prompts/`, not strings hidden in a framework or in code. Changing a prompt is a reviewed change to a file. |
| 3 | Own your context window | `t12 context` builds the worker's context deterministically from the run record: task, ACs, phase, budgets left, changed files, last compacted error. Nothing else is smuggled in. |
| 4 | Tools are just structured outputs | Each tool is a CLI command with typed arguments (argparse); a step that does not fit the phase, session, or evidence is refused, never guessed at. **Not yet built:** a JSON result schema (`tool`, `args`) for worker outputs. |
| 5 | Unify execution state and business state | One run record per task holds both: phase, budgets, gates, evidence refs (execution) and task, ACs, candidate commit (business). Plus an append-only `events.jsonl`. State is derived by folding the record, never from a second store. |
| 6 | Launch / Pause / Resume with simple APIs | `prepare`, `pause`, `resume`, `status` are plain CLI commands. Resume re-validates the run from disk (HEAD, test blob, evidence hashes) before continuing; drift fails closed. |
| 7 | Contact humans with tool calls | `escalate --question ...` is a tool call the worker (or operator) makes. It records the question, pauses the run, and waits. The factory never auto-answers a human question. **Not yet built:** recording the human's answer and passing it to the next `t12 context`. |
| 8 | Own your control flow | The state machine in `t12/model.py` is the only control flow. Transitions are enumerated; an invalid transition is an error, not a prompt decision. |
| 9 | Compact errors into context window | Raw failure output lives in evidence; the next worker context gets a compacted summary (last ~40 lines + exit code + which gate). The full log is never pasted into a prompt. |
| 10 | Small, focused agents | One worker = one task = one role (builder, test-writer, reviewer, QA). Each role has its own prompt and its own session id, and a session serves one role per run. All roles share the run's one worktree, so separation is by session, not by filesystem. |
| 11 | Trigger from anywhere, meet users where they are | Entry points are a CLI and a plain JSON task file. No server, no dashboard needed to launch. A chat message, a cron job, or a terminal can all produce the same task file and run the same commands. |
| 12 | Make your agent a stateless reducer | Each CLI invocation is `f(state on disk, event) -> new state on disk`. Nothing lives only in a process's memory between steps. Any step can be replayed from the run record + event log. |

Honourable mention (Factor 13, pre-fetch context): `prepare`
validates the repo, base, branch, and gate commands up front, so a
run never fails halfway for a missing prerequisite it could have
checked at admission.

## Super-board, as adopted (not copied)

Source: `EricTechPro/super-board`, skill `super-review`
(`skills/super-review/SKILL.md`). Turbo's DESIGN-SPEC adapted its
skill ecosystem and reviewer workflow; we adopt the same ideas,
without its host-specific hooks, GitHub Project board, or merge
permissions.

- **Orchestrator dispatches roles, it does not implement.**
  `skills/orchestrator/SKILL.md`: builder -> QA -> security ->
  independent reviewer, bounded feedback routing, one lane at a time.
- **Reviewer forms its own hypotheses first.** Read the ACs and the
  raw diff, write 2–4 hypotheses about what must be true and where it
  would break, *then* read the builder's summary as claims to check.
  A claim is a lead, not evidence.
- **Findings are classified, with stable ids:** Gap, Bug,
  Verification miss, Scope drift, Over-engineering (should-fix at
  most, never blocks alone). Severity: Blocker / Should fix / Nit /
  Human gate. Deepening opportunities become future tickets, never a
  reason to hold a green PR.
- **Review remembers.** A re-review starts from the prior report:
  each prior finding is re-checked with file:line proof and marked
  fixed / not fixed / no longer applies. A "resolved" thread or a
  builder's reply is not evidence; read the code.
- **Reviewer re-runs verification itself.** It re-runs the exact
  recorded test command in a clean worktree. Green continues; red
  bounces to QA with the failure quoted.
- **Merge is pinned to the reviewed head SHA.** The merge gate
  refuses if the head moved after review. In Phase 1 there is no
  merge command at all: `local-verified` is the end of the factory's
  authority. A human merges. (Turbo's DESIGN-SPEC lets child PRs
  merge into a feature branch without human approval; that is a
  Phase 7 delivery decision for Michael to make, not a default here.)
- **Human gates are explicit.** Money, auth, destructive schema,
  live DB, or any product decision the reviewer cannot safely guess
  goes to a human with a named unblock step. The run pauses; it does
  not guess.

## TypeScript and UI work: anti-slop

Source: https://github.com/miqdadbadjuber/anti-slop (MIT).
Michael's pointer: Dex Horthy's 12-factor-agents talks frame the same
problem as a "war on slop" — unattended agents ship generic UI, hype
copy, and AI-shaped code, and slop compounds because agents imitate
the repo's existing patterns. (Verified: the repo is real, ~8.9k
stars, v3.2.28; what I could not verify from the 12-factor README is
that Dex name-checked this specific repo — recording it as Michael's
pointer, not as a Dex citation.)

- **What it is:** a filter, not a style guide. 40 mandatory rules
  (R-01–R-40) in three tiers, a Liveliness Toolkit, and a Delivery
  Gate — a mandatory PASS/FAIL report before anything ships. Six
  skills, load only what the task needs: `antislop` (core, always),
  `antislop-ui`, `antislop-copywriting`, `antislop-human`
  (accessibility), `antislop-layoutmobile`, `antislop-code`.
- **When it applies:** any task whose deliverable is TypeScript code
  or UI — which is the likely shape of product work here (TypeScript
  on Node, React + TypeScript with Vite+ for UI/tests, Electron
  shell for Chat Bridge). The Python core of turbo-12f itself does
  not need it; the factory's product work does.
- **How it runs:** two modes — `during` (rules guide the build, the
  session closes with the Delivery Gate report) and `after` (audit
  existing work, numbered findings, fix what the human approves).
  Default for factory work is `during`.
- **The direction rule (matters):** anti-slop removes slop and stops
  there; visual direction comes from a per-product UI direction file
  (also called `DESIGN.md` in that repo). Without it, UI output is
  labelled a draft without direction. That file is **not**
  turbo-12f's `DESIGN.md` — same name, different file, different
  job. The direction file belongs to the product repo and the task
  contract (Factor 2: own your prompts; the task supplies direction,
  the worker does not invent it).
- **Not vendored:** turbo-12f does not copy the skill in. The worker
  loads it per task: `npx antislop-ai` (installer),
  `npx skills add miqdadbadjuber/anti-slop`,
  `agy plugin install https://github.com/miqdadbadjuber/anti-slop`,
  or the single `antislop.md` file. Pin a version when adopting;
  the repo does not update itself.
- **Reviewer check:** the reviewer verifies the Delivery Gate report
  exists and passed. A missing or failing gate is a Verification
  miss; the run is unverified until it passes.

## Supersession and stop (Phase 2)

Runs are never overwritten and never silently reset. Stopping is an
explicit, recorded act: `stop --reason ...` moves any active phase
(including `paused`) to `stopped` with the reason in the record and
the event log. A stopped run cannot move again.

Starting over on the same task uses supersession, not reset:
`prepare --supersede` requires the existing run to be `stopped`
(active runs are refused with "stop it first"), archives the old run
record and its event log under a timestamped name
(`runs/<id>.superseded-<ts>.json`), removes the old disposable
worktree and factory branch, and records `supersedes` in the new run.
The archived record keeps every SHA and evidence ref, so the old
attempt stays inspectable. Counters (ops, repairs, wall time) start
fresh in the new run — the new attempt is a new attempt, not a
continuation that hides its history.

Resume consistency: `resume` re-validates from disk before returning
to the prior phase — worktree branch, HEAD against the expected
commit for that phase, red test blob, and every recorded evidence
ref re-hashed. Any drift fails closed and the run stays paused.

## Safe execution (Phase 3)

Enforced by the core, deterministically:

- **Forbidden bypass flags.** `--dangerously-skip-permissions`,
  `--no-sandbox`, `--bypass-sandbox` are rejected at admission
  (gate commands) and at launch (every command the runner starts,
  token-based so a path or code string containing the text does not
  trip it). A task that needs one cannot be admitted.
- **Credential stripping.** Every subprocess environment drops
  credential variables (exact names like `GITHUB_TOKEN`,
  `AWS_SECRET_ACCESS_KEY`, plus patterns for `*TOKEN*`, `*SECRET*`,
  `*PASSWORD*`, `*API_KEY*`, `*PRIVATE_KEY*`) and git-redirecting
  variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_SSH_COMMAND`,
  `GIT_CONFIG_*`, `PYTHONPATH`, `LD_PRELOAD`, …), and sets
  `GIT_ALLOW_PROTOCOL=none`.
- **Empty verify-home.** Verification commands (red, gate, replay,
  probe) run with `HOME` set to an empty, private directory inside
  the state home, `TMPDIR` inside it, and no system git config — so
  no operator credential store or global config is visible.
- **Landlock cage (Linux).** Where the kernel supports it (ABI ≥ 4),
  verification commands run under Landlock: worktree and verify-home
  writable, system dirs read-only, `/dev` device nodes available,
  all TCP bind/connect denied — including loopback. Verified on this
  machine: a sandboxed command cannot write outside the worktree or
  connect anywhere, while normal commands in the worktree are
  unaffected. Git operations (commit, stage, worktree management)
  run outside the cage so the factory can still record evidence.
- **Docker adapter.** A task may request `"sandbox_adapter":
  "docker"` (or the operator sets `T12_SANDBOX_ADAPTER=docker`);
  verification commands then run as `docker run --rm --network none`
  with the worktree and the verify-home mounted at their host paths,
  `--user` set to the host uid/gid (so files created in the worktree
  are owned by the host user), and only the verification env passed
  as `-e` flags — never the host's environment, so no secret can
  ride in. The container env adds two factory-controlled values:
  `GIT_ALLOW_PROTOCOL=none` and `PYTHONDONTWRITEBYTECODE=1` (without
  the latter, the verification commands themselves dirty the
  worktree with `__pycache__` and green refuses). The image comes
  from the task's `"sandbox_image"` (default `python:3.12-slim`, or
  `T12_SANDBOX_IMAGE`); it must already be present locally —
  `prepare` refuses when the daemon does not answer or the image is
  missing, so there are no implicit pulls mid-run. The adapter is
  chosen at admission and recorded on the run; every verification
  command and every probe uses that adapter, and the manifest lists
  it with the image. Added 2026-11-11; tested here against a fake
  `docker` stub (`tests/fake_docker.py`) because this machine has
  no Docker — the stub emulates the daemon and stands in for the
  container's confinement with a Landlock cage. Real end-to-end
  verification needs a machine with Docker installed.

Measured, not asserted — the probes:

- **Required probes** run at `prepare` and on demand (`t12 probe`):
  `forbidden_credential_access` (planted secret env var + sentinel
  file + home credential store must be unreadable) and
  `external_effect_restriction` (network connect + write outside the
  worktree must be denied). Each attempt is recorded as evidence
  (`probe-<name>-output.txt`, `probes.json`); a granted access fails
  the probe and is reported, not hidden.
- **Adapter detection.** `landlock` on Linux with a new-enough
  kernel, `docker` when the task or environment requests it and the
  daemon answers, `sandbox-exec` detected on macOS but not enforced
  there (unverified), `none` otherwise. When no adapter is available,
  verification runs unwrapped and the probes record the gap. The
  probes' outside-write target is the state home root (not `/tmp`):
  `/tmp` is the container's own ephemeral directory under Docker,
  so writing it would not test confinement at all.
- **Opt-in hard gate.** A task may set `"require_probes": true`;
  then red, gate, and replay refuse until probes have passed for
  the run, and `prepare` stops the run if probes fail at admission.
  Default is off: probes are evidence, and the manifest lists the
  adapter and every probe result.

What this does not claim: `local-verified` never means "isolated".
Isolation is whatever the adapter on this host enforces, and the
manifest says which. Tests that need a loopback server will not
pass under the cage — that is the cage working, and such tests
belong outside the verification gates or in a task that does not
run them sandboxed.

## Evidence integrity (Phase 4)

The run record is a plain JSON file, and a plain file can be
edited. Phase 4 makes edits detectable instead of pretending they
cannot happen:

- **Hash-chained event log.** Every event carries a sequence
  number, a link to the previous event's hash, and a digest of the
  run record as it stood after that command (`state_sha`). Editing,
  deleting, or reordering an event breaks the chain; editing the
  run record after the last event breaks the state digest. The
  first event links to a per-task genesis hash. A corrupted log
  fails closed: further commands refuse until the run is superseded.
- **`t12 verify` — the auditor.** Five checks, each PASS/FAIL, exit
  0 only if all pass: the event chain; every evidence ref re-hashed
  (red, replay, gate and probe observations); the exit codes (a red
  must have failed, a replay and every passed gate must have exit
  0, and the recorded code must agree with the stored observation);
  HEAD pinned to the expected commit and the red test blob
  unchanged; the manifest re-hashed against its issued hash.
- **Certification checks the chain too.** `green` refuses on a
  broken chain before it looks at any evidence, and `resume`
  refuses on a broken chain before returning to the prior phase.
  `verify_green` also checks the exit-code story: a gate flipped
  to "passed" in the record while its observation shows a failure
  is refused, and a replay recorded against the wrong commit is
  refused.
- **Manifest issuance is recorded.** Writing a manifest appends a
  `manifest` event with its SHA-256 (or the outside path for
  `--out`), so a rewritten manifest is detectable against the log.
  The `green` certification already recorded the manifest hash in
  its `verified` event.

What this is not: there is no signing key and no external
notary. Anyone with write access to the state home *and* the
patience to rewrite the whole chain from genesis can forge a
history — the chain detects edits, it does not prevent a full
rewrite by the machine's owner. That is the honest boundary:
tamper-evidence for accidents and for a compromised worker, not
tamper-proofing against the operator. One corollary, found while
testing Phase 6: a later command absorbs a prior record edit into
the chain (its event's state digest is computed over the edited
record), so the chain only detects edits made after the *last*
event. The exit-code and evidence checks still catch a flipped
gate or a changed test blob even after absorption.

## Limits (Phase 5)

The built-in defaults (3600s, 100 ops, 3 repairs) were guesses.
Phase 5 replaces guessing with measurement:

- **Measurements at terminal transitions.** When a run reaches
  `local-verified`, is stopped, or stops on the repair cap, the
  record snapshots what it actually used: wall time, ops, repairs,
  gate count. The manifest shows the measured usage next to the
  budget. A run without measurements cannot calibrate anything and
  is reported as such.
- **`t12 report` — budget reporting.** Per-run usage vs budget,
  cap hits (repairs / ops / time), and aggregates over completed
  runs. `--json` prints the same structure for scripts.
- **`t12 calibrate` — defaults from real runs.** From completed
  runs only (`local-verified` with measurements): the recommended
  default for each budget is the p90, rounded up to a sane step
  (60s / 5 ops / 1 repair), never below a floor (600s / 20 ops /
  2 repairs). Fewer than 3 completed runs, and it refuses — one
  toy run must not set the defaults. Runs stopped by the repair
  cap are censored: their true need is unknown, so they are listed
  as excluded rather than folded into the numbers. Dry run by
  default; `--apply` writes `limits.json` with the recommendation
  and its provenance (sample size, stats, time).
- **Precedence, recorded.** A task's own budgets always win;
  calibrated defaults from `limits.json` apply when the task omits
  a budget; built-ins are the last resort. The run record stores
  `budget_source` (`task` / `calibrated` / `builtin`) so any run
  can say where its limits came from. A malformed `limits.json`
  fails `prepare` closed rather than being silently ignored.

What this does not do: it does not auto-tune anything. Calibration
is an explicit operator act (`calibrate --apply`), and the
recommendation is a starting point the operator can edit — the
file is plain JSON. Limits are per state home, not per product:
different products' runs should live in different state homes if
their shapes differ.

## Roles (Phase 6)

Phases 1–5 built the deterministic core; Phase 6 wires the people
(and their agents) into it. The core still makes no model calls —
the roles are separate worker invocations by the orchestrator, and
the core's job is to make the separation checkable:

- **One session per role.** Every role step carries a session id
  (`--session` on `red`, `stage`, `gate`, `review`). A session id
  may serve exactly one role per run; the factory refuses a
  session already bound to another role. The test-writer's session
  cannot stage, the builder's session cannot review, the QA
  session cannot do anything else on the run. The same session
  may repeat the same role (a re-review, a second gate) — that is
  the same worker continuing, not a collapse of separation.
- **The review is a command, not a comment.** `t12 review --role
  reviewer|security --session <id> --verdict
  approve|changes-requested --report <file>` runs after all gates
  pass (phase `gates` or `green`), on the current candidate. The
  report file is copied into evidence and hashed; the review
  record binds role, session, verdict, candidate commit, report
  ref, and the prior review for that role. `verify` re-hashes the
  report like any other evidence.
- **Green requires approval.** `green` refuses without an
  approving review for every role in the task's
  `required_reviews` (default `["reviewer"]`; a task can add
  `"security"` or set `[]` to opt out) on the current candidate.
  A `changes-requested` review newer than the latest approval
  blocks. Evidence failures are still checked first, so a
  tampered gate or test blob surfaces before the review policy.
- **Re-stage voids reviews.** A new candidate resets gates,
  clears the replay, and marks prior reviews voided — they stay
  in the record as the audit trail. The state machine allows
  green → gates for this loop (Phase 2's matrix was amended
  deliberately, spec and implementation together). The next
  review for the role records the voided report as
  `prior_review`, and `t12 context --role reviewer` surfaces it:
  review remembers is state the factory carries forward, not a
  note the reviewer has to find.
- **Security is a review role, not a gate.** It has its own owned
  prompt (`prompts/security.md`) and skill (`skills/security/`),
  the same report + verdict protocol, and its own findings
  vocabulary (Secret / Injection / Auth / Exposure / Dependency).
  A task declares it in `required_reviews` when it matters.

What the factory cannot prove: session ids are claims. Two
different ids could in principle be the same worker, and the core
has no way to know. The enforcement is over the claim, and the
claim is recorded. If the host cannot be trusted to hand out
distinct sessions per role, the separation is only as strong as
the host — that is stated here rather than papered over.

## Delivery (Phase 7)

Delivery is where the factory meets the outside world, so it is
where the discipline matters most. The split is strict:

- **The core never touches the network.** It holds no tokens and
  makes no HTTP calls. `t12 deliver --adapter github|asana
  --report report.json [--resolves]` only records a report an
  adapter already wrote: it checks the report's shape, pins it to
  the reviewed head, enforces the uncertain block, stores the
  report as hashed evidence, and appends a `delivered` event.
  `verify` re-hashes delivery reports like any other evidence,
  and `status` lists the deliveries.
- **Adapters do the remote work.** `adapters/github.py create-pr`
  and `adapters/asana.py complete` are standalone stdlib-only
  scripts. Tokens come from the environment (`GITHUB_TOKEN`,
  `ASANA_TOKEN`); the repo comes from `--repo`, the task's
  `github_repo` field, or the origin remote; the Asana task from
  `--task` or the task's `asana_task_gid` field. An adapter exits
  0 when it wrote a report, whatever the outcome — a non-zero
  exit means the adapter itself could not run.
- **Outcomes mean what they say.** `confirmed` only when the
  remote state was read back and matches: the PR exists with the
  reviewed head SHA, the Asana task is complete. `rejected` when
  the remote definitively said no (the PR's head is a different
  commit, the branch is not pushed, the task does not exist).
  `denied` when the mutation was refused for permission (403 on
  the create/complete call). `uncertain` when the state is
  unknown (timeout, 5xx, a read that failed).
- **Never a blind retry.** An uncertain outcome blocks further
  delivery for that adapter. The next attempt must be a read-back
  recorded with `--resolves`, not a repeated action — a blind
  retry after an uncertain mutation can duplicate the effect (a
  second PR, a second comment) or miss that the first one landed.
  The block is per adapter; one adapter's uncertainty does not
  stop the other.
- **Two mislabelings are ruled out** — both were real findings in
  the original Turbo's integration work. A 401/403 on a READ is
  uncertain, never denied: the adapter could not establish state,
  and denied means a mutation was refused. A 5xx during
  confirmation is uncertain, never rejected: rejected means the
  remote definitively said no. The adapters classify by
  read-vs-mutation, and the tests pin both cases.
- **No merge.** The GitHub adapter creates the PR and reads it
  back; it never merges, and there is no merge command. A human
  merges. Child-PR auto-merge is not a default — enabling it
  would be a delivery decision for the maintainer, not something
  this phase assumes.

What the factory cannot prove: the adapter's report is a claim.
The core checks the report's shape and its head pinning, and the
report is stored as evidence — but if an adapter lies about
having read the remote back, the core cannot tell. The defense is
that the adapter is small, stdlib-only, and its read-back logic
is the part the tests exercise against a fake server; and the
ref (PR URL, Asana permalink) is in the record for a human to
open.

## Layout

```text
bin/t12                  launcher (resolves the package from its own path)
t12/
  cli.py                 commands: init/prepare/red/stage/gate/replay/
                         review/green/manifest/status/pause/resume/
                         escalate/context/verify/probe/report/calibrate/
                         deliver
  delivery.py            delivery outcomes, head pinning, uncertain block
  model.py               phases, transitions, budgets, run record (Factor 5/8/12)
  runner.py              safe subprocess: argv only, timeout, kill group,
                         sanitized env, duration + exit captured
  gitio.py               git invariants: clean tree, blob hash, ancestry,
                         worktrees, commits
  store.py               atomic writes, private perms, symlink rejection
  evidence.py            sha256 refs, re-hash verification, provenance
  gates.py               ordered gate ladder, repair cap
  limits.py              budget reporting, p90 calibration, limits.json
  sandbox.py             forbidden flags, credential stripping, Landlock cage, Docker adapter, probes
  errors.py
prompts/                 owned prompts, versioned (Factor 2)
skills/                  orchestrator, builder, reviewer, qa, security (super-board roles)
adapters/                delivery adapters (github, asana) + shared HTTP/outcome rules
tests/                   unittest, synthetic git repos, fake HTTP servers, no network, no model
scripts/smoke.sh         happy-path smoke on a disposable repo
examples/                a sample task file
```

## Truth boundaries (carried over from Turbo)

- The factory is not a coding agent and not a sandbox. A worktree
  isolates files, not process privileges. Protections must be
  tested, not inferred from instructions.
- Host declarations (model name, sandbox claim, cost) are not
  verified facts. Spend is reported usage, estimated cost, or
  unknown — never presented as verified billing.
- `local-verified` is not QC approval or product acceptance.
- Evidence stored outside the state home, or reachable through a
  symlink, is rejected. A same-user process can forge local files;
  this is an honesty and reproducibility layer, not a security
  boundary against a malicious local actor.

## Roadmap (phases from Michael's Turbo mapping)

1. **Minimal evidence runner** — this scaffold: init, prepare, red,
   stage, gate, replay, green, manifest, status, pause/resume.
2. **State machine hardening — DONE 2026-11-11.** Property tests
   on transitions (`tests/test_model.py`: the matrix is written as
   an independent spec, so weakening a transition fails the test),
   explicit `stop`, supersession instead of unsafe reset
   (`prepare --supersede` archives the old run + events and deletes
   the disposable worktree/branch; active runs must be stopped
   first), and resume re-validates HEAD, test blob, *and* every
   evidence hash before continuing.
3. **Safe execution — DONE 2026-11-11.** Forbidden bypass flags
   (`--dangerously-skip-permissions`, `--no-sandbox`,
   `--bypass-sandbox`) rejected at admission and at launch;
   credential/git-redirecting env vars stripped from every
   subprocess; verification commands run with an empty verify-home
   and, where the OS supports it, under a Landlock cage (worktree +
   verify-home writable, system dirs read-only, all TCP denied —
   verified on this machine: localhost connect and /tmp write both
   denied, normal commands unaffected); two required safety probes
   (`forbidden_credential_access`, `external_effect_restriction`)
   run at `prepare` and on demand via `t12 probe`, recorded as
   evidence; a task can set `require_probes` to hard-gate
   red/gate/replay on passing probes.
4. **Evidence integrity — DONE 2026-11-11.** Hash-chained event
   log: every event links to the previous hash, carries a sequence
   number, and records a digest of the run record as it stood after
   that command. `t12 verify` audits the chain, re-hashes every
   evidence ref, checks the recorded exit codes tell the same story
   as the statuses (a flipped gate status with a failing
   observation is refused by `green`), re-pins HEAD/test-blob, and
   checks the manifest against its issued hash. `green` and
   `resume` refuse on a broken chain; manifest issuance is itself a
   recorded event. No signing key: the chain detects edits, it does
   not prevent a full rewrite by the machine's owner.
5. **Limits — DONE 2026-11-11.** Measurements are recorded at
   every terminal transition (wall time, ops, repairs, gates).
   `t12 report` shows per-run usage vs budget, cap hits, and
   aggregates; `t12 calibrate` recommends defaults from the p90 of
   completed runs (floors: 600s / 20 ops / 2 repairs; minimum
   sample 3; repair-cap-stopped runs censored, not folded in) and
   writes them to `limits.json` with `--apply`. `prepare` uses a
   task's own budgets first, calibrated defaults next, built-ins
   last, and records which source applied.
6. **Roles — DONE 2026-11-11.** Each role runs as a separate
   worker invocation with its own session id (`--session` on red,
   stage, gate, review). One session serves exactly one role per
   run — the factory refuses a session already bound to another
   role, so the test-writer cannot stage, the builder cannot
   review, and the reviewer cannot do anything else on the run.
   `t12 review` (reviewer / security) records a report as
   evidence, bound to the candidate commit; `green` requires an
   approving review for every role the task lists in
   `required_reviews` (default `["reviewer"]`) on the current
   candidate. A re-stage voids prior reviews and resets gates +
   replay — the state machine now allows green → gates for the
   review loop — and the next review starts from the voided report
   (review remembers, via `t12 context --role reviewer`).
7. **Delivery — DONE 2026-11-11.** The core never talks to the
   network and holds no credentials: adapters (`adapters/github.py`,
   `adapters/asana.py`, stdlib-only) do the remote work and write a
   JSON report, and `t12 deliver` records the report as evidence.
   Delivery needs a `local-verified` run with an intact chain and
   HEAD on the candidate, and is pinned to the reviewed head SHA —
   a report for any other commit is refused. Outcomes are exactly
   `confirmed` (remote state read back and matching), `rejected`
   (remote definitively said no), `denied` (mutation refused for
   permission), `uncertain` (state unknown). An uncertain outcome
   blocks further delivery for that adapter until an explicit
   `--resolves` read-back report arrives — never a blind retry.
   The adapter never merges; a human merges the PR.
