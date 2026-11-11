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
| 1 | Natural language to tool calls | The LLM's only job is to turn an owned prompt + context into a structured result: file edits in the worktree, or a JSON tool call (`escalate`, `done`). It never runs commands, never merges, never decides the phase. `prompts/*.md`, `t12/model.py` (result schema). |
| 2 | Own your prompts | Prompts are versioned files under `prompts/`, not strings hidden in a framework or in code. Changing a prompt is a reviewed change to a file. |
| 3 | Own your context window | `t12 context` builds the worker's context deterministically from the run record: task, ACs, phase, budgets left, changed files, last compacted error. Nothing else is smuggled in. |
| 4 | Tools are just structured outputs | Worker outputs are validated JSON against a schema (`tool`, `args`). Free text is never parsed as an instruction. A malformed result is a failed step, not a guess. |
| 5 | Unify execution state and business state | One run record per task holds both: phase, budgets, gates, evidence refs (execution) and task, ACs, candidate commit (business). Plus an append-only `events.jsonl`. State is derived by folding the record, never from a second store. |
| 6 | Launch / Pause / Resume with simple APIs | `prepare`, `pause`, `resume`, `status` are plain CLI commands. Resume re-validates the run from disk (HEAD, test blob, evidence hashes) before continuing; drift fails closed. |
| 7 | Contact humans with tool calls | `escalate --question ...` is a tool call the worker (or operator) makes. It records the question, pauses the run, and waits. The factory never auto-answers a human question. |
| 8 | Own your control flow | The state machine in `t12/model.py` is the only control flow. Transitions are enumerated; an invalid transition is an error, not a prompt decision. |
| 9 | Compact errors into context window | Raw failure output lives in evidence; the next worker context gets a compacted summary (last ~40 lines + exit code + which gate). The full log is never pasted into a prompt. |
| 10 | Small, focused agents | One worker = one task = one role (builder, test-writer, reviewer, QA). Each role has its own prompt, its own input contract, and no access to another role's worktree. |
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

## Layout

```text
bin/t12                  launcher (resolves the package from its own path)
t12/
  cli.py                 commands: init/prepare/red/stage/gate/replay/
                         green/manifest/status/pause/resume/escalate/context
  model.py               phases, transitions, budgets, run record (Factor 5/8/12)
  runner.py              safe subprocess: argv only, timeout, kill group,
                         sanitized env, duration + exit captured
  gitio.py               git invariants: clean tree, blob hash, ancestry,
                         worktrees, commits
  store.py               atomic writes, private perms, symlink rejection
  evidence.py            sha256 refs, re-hash verification, provenance
  gates.py               ordered gate ladder, repair cap
  errors.py
prompts/                 owned prompts, versioned (Factor 2)
skills/                  orchestrator, builder, reviewer, qa (super-board roles)
tests/                   unittest, synthetic git repos, no network, no model
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
3. **Safe execution** — sandbox adapters, safety probes (attempt the
   forbidden access, verify it is denied).
4. **Evidence integrity** — signed manifests, config digests across
   runs, tamper drills.
5. **Limits** — calibrated defaults from real runs, budget reporting.
6. **Roles** — builder / test-writer / QA / security / reviewer as
   separate worker invocations with their own prompts, worktrees,
   and evidence; review loop with prior-report memory.
7. **Delivery** — GitHub/Asana adapters last, merge gate pinned to
   head SHA, merge outcomes `confirmed / rejected / denied /
   uncertain`, never a blind retry on uncertain.
