# Generic factory: one factory, many projects

Status: steps 1 (project profile) and 2 (roles and skills) are built;
the rest is a proposal.
README.md describes what is enforced today.

## Goal

turbo-12f should deliver any project, not only chat-bridge. A
project tells the factory how it works through one config file. The
factory keeps the evidence chain (red, stage, gates, replay, review,
green) the same for every project.

## What is project-specific today

The core has no chat-bridge strings. Before step 2, five
things were fixed in code and only changed by editing the factory.
Two still are:

| Concern | Where it is fixed today |
|---|---|
| Task intake | hand-written `task.json` only; every task repeats gates, sandbox, `verify_deps` |
| Delivery | `ADAPTERS` in `t12/delivery.py`; GitHub and Asana scripts only |

chat-bridge works around this with its own in-repo factory
(`chat-bridge/factory/`), which hardcodes `PROTECTED_PATHS`, its
gate ladder, and its lanes. Before the project profile, turbo-12f had
no protected-path check at all, so it could not enforce the rule that `guards/`, `e2e/`,
`bench/`, and `docs/health/` are read-only to the builder.

## Data shape

### Project profile

The profile is `.t12/project.json` in the product repo.

```json
{
  "version": 1,
  "name": "chat-bridge",
  "base": "main",
  "gates": [
    {"name": "lint", "command": ["ruff", "check", "."]},
    {"name": "unit", "command": ["python3", "-m", "pytest", "tests/unit", "-q"]},
    {"name": "integration", "command": ["python3", "-m", "pytest", "tests/integration", "-q"]}
  ],
  "verify_deps": [".venv"],
  "sandbox": {"adapter": "docker", "image": "chat-bridge-verify:1"},
  "protected_paths": ["guards/", "e2e/", "bench/", "docs/health/", "AGENTS.md", ".t12/"],
  "context_docs": ["AGENTS.md", "docs/spec.md", "docs/architecture.md"],
  "roles": {
    "test-writer": {"kind": "test-writer"},
    "builder": {"kind": "builder", "skills": ["plain-ui-copy"]},
    "reviewer": {"kind": "review"},
    "security": {"kind": "review"},
    "ux-copy": {"kind": "review", "prompt": "ux-copy.md"}
  },
  "required_reviews": ["reviewer", "ux-copy"],
  "intake": {"adapter": "github-issues", "repo": "owner/chat-bridge", "label": "factory"},
  "delivery": [{"adapter": "github", "repo": "owner/chat-bridge"}]
}
```

Rules that follow from this shape:

- **The profile is read from trunk, not the task's base or the worktree.**
  `prepare` reads `.t12/project.json` at the trunk commit the state
  home names, validates it, and freezes the merged settings and the
  trunk commit in the run record. A task cannot choose an older or
  unmerged commit to get weaker rules, and a builder that edits
  `.t12/` changes nothing for its own run.
- **A task file only holds what is unique to the task.** `id`,
  `title`, `acceptance_criteria`, and an optional `source` ref. Any
  profile key a task sets overrides the profile for that run, and
  the override is recorded.
- **`protected_paths` becomes a stage check.** `stage` refuses a
  candidate whose diff from base touches a protected path. `.t12/`
  is always protected, so a run cannot rewrite its own rules.
- **No profile means today's behaviour.** The built-in profile is
  the current defaults: roles test-writer, builder, qa, reviewer,
  security; `required_reviews: ["reviewer"]`; no intake; delivery
  through the command line.

### Roles are named; kinds are fixed

A role is a name the project picks. A kind is what the core knows
how to enforce. There are four kinds:

| Kind | Tool it may call | What the core does with it |
|---|---|---|
| `test-writer` | `red_test_ready` | runs red |
| `builder` | `implementation_ready` | stages a candidate |
| `qa` | `qa_report` | records the report |
| `review` | `review` | records a verdict; `required_reviews` names which review roles green needs |

All kinds may call `escalate`. `TOOLS` is keyed by kind. `prepare`
merges the built-in roles (test-writer, builder, qa, and reviewer and
security as `review`) with the profile's `roles`, and freezes the
result in the run record as `role_map`. `act`, `review`, `context`,
session binding, and `required_reviews` all check roles against that
map. A project may add roles and set a prompt or skills on a
built-in, but may not change a built-in's kind. A task file may not
set `roles`. The one-session-per-role rule is unchanged. A
project adds a reviewer (UX copy, accessibility, data privacy) by
adding a `review` role with its own prompt. It needs no change to
the core.

### Prompts and skills resolve in layers

`t12 context --role R` builds the prompt from:

1. The role's prompt: `.t12/prompts/<file>` at the trunk commit if
   the profile names one, else the factory's prompt. A built-in role
   keeps its file (`prompts/reviewer.md`, `prompts/security.md`); a
   project role gets the prompt of the first built-in role of its
   kind, so a `ux-copy` review role starts from `prompts/reviewer.md`.
2. Each skill the role lists: `.t12/skills/<name>/SKILL.md` at the
   trunk commit, else the factory's `skills/<name>/SKILL.md`. A skill
   found in neither place is an error, at `prepare` and at `context`.
3. `context_docs`, as paths the worker must read. They are listed,
   not inlined, so the context stays small (Factor 3). Not built yet.

The context JSON's `instructions` list records the source (`project`
or `factory`), path, and SHA-256 of every prompt and skill it used,
so a review can tell which instructions a worker saw.

### Intake and delivery are adapters with a fixed contract

Adapters stay separate programs that hold credentials. The core
never imports one (this is unchanged).

- **Intake** (`t12 intake --home H`) runs the profile's intake
  adapter. The adapter writes task files in the task schema, each
  with `source: {"adapter": "github-issues", "ref": "...", "url": "..."}`.
  `prepare` admits them as it admits a hand-written task. Planned
  adapters are `file`, `github-issues`, `gitlab-issues`, `jira`,
  `linear`, and `asana`.
- **Delivery** keeps today's report schema and four outcomes. The
  `ADAPTERS` tuple is replaced by the profile's `delivery` list.
  `deliver` refuses an adapter the frozen profile does not name.
  Planned change-request adapters are `github` (exists), `gitlab`
  (merge request), and `bitbucket` (pull request). They share one
  contract: read the branch, find or create the change request,
  read it back with the reviewed head, and never merge. The
  change-request body links `source.url`, so the issue closes
  through the forge's own syntax.
- **Adapters come from the factory or the state home, never the
  product repo.** An adapter runs on the host with tokens. Loading
  one from the repo the agents write to would let a worker run code
  with those tokens. A project that needs a custom adapter puts it
  in `<state-home>/adapters/` and names it in the profile.

## Workflow: as configurable as the invariants allow

Rule: a project configures the data the phase machine reads. It never
configures the machine. Every item below is data; every fixed item
names the factor or fix it protects.

A project can set:

- the gate ladder: which checks run, their commands, their order
- its own roles, each mapped to a kind, with any number of review
  roles and the set green requires
- prompts and skills per role, from `.t12/` before the factory's
- whether a task may waive red, with a reason the manifest shows
- whether the QA lane runs
- pause points where a human must resume, such as before green or
  before delivery (Factor 6 and 7)
- budgets, sandbox, `verify_deps`, protected and security-review
  paths, `touches` and `depends_on`
- intake and delivery adapters
- how often a pattern must recur before `retro` reports it

A project cannot change:

- the phase order: red, stage, gates, replay, review, green. The
  state machine is the only control flow (Factor 8).
- one JSON tool call per worker, with a fixed tool set per kind, so
  every tool maps to a step the core checks (Factor 4)
- one session per role (Factor 10)
- that the profile comes from trunk and is frozen at
  `prepare`, and that a change to `.t12/` needs a security review.
  This is "the candidate never checks itself" for project settings.
- that `touches` is enforced at `stage`; without it the queue's
  no-overlap promise is a claim
- that the core never calls a model or the network and never merges;
  `retro` and `calibrate` propose, a human applies

## Plan

Each step is one PR on top of the last. Each ends in a check that
runs against a disposable repo, as the current tests do.

1. **Project profile. Built.** `.t12/project.json` replaces
   `.t12/policy.json`. Read from the configured trunk, merged under
   tighten-only rules, protected paths refused at `stage`.
2. **Roles and skills. Built.** Role-to-kind map, `TOOLS` keyed by kind,
   layered prompt and skill resolution, hashes in the context. The
   check is a project-defined `ux-copy` review role that blocks
   green until it approves.
3. **Delivery adapters.** The profile's delivery list replaces
   `ADAPTERS`. GitLab and Bitbucket adapters, tested against fake
   HTTP servers with the existing outcome rules (a failed read is
   uncertain, never denied).
4. **Intake adapters.** `t12 intake`, then a `source` ref carried
   from the issue to the change request. GitHub issues and GitLab
   issues first.
5. **Onboard chat-bridge.** Write its `.t12/project.json` from
   `chat-bridge/factory/paths.py` and its gate ladder. Run one real
   issue end to end. Then retire `chat-bridge/factory/` in a
   chat-bridge PR.

## Open questions

- **Gate ladders per task type.** chat-bridge has QC-owned gates 4
  to 6 that run in CI, not locally. The proposal records them as
  out of scope for `local-verified`. A project could instead declare
  `ci_gates` that delivery reads back from the forge.
- **Model invocation.** The core still never calls a model.
  A runner that calls `claude -p` per role would make the factory
  run on its own. That runner belongs in `adapters/`, not the core,
  and is not in this plan.
