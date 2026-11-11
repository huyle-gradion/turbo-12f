# Generic factory: one factory, many projects

Status: proposal, not built. Nothing in this file is enforced yet.

## Goal

turbo-12f should deliver any project, not only chat-bridge. A
project tells the factory how it works through one config file. The
factory keeps the evidence chain (red, stage, gates, replay, review,
green) the same for every project.

## What is project-specific today

The core has no chat-bridge strings, but five things are fixed in
code and only change by editing the factory:

| Concern | Where it is fixed today |
|---|---|
| Roles | `ROLES` in `t12/cli.py`, `SESSION_ROLES` and `REVIEW_ROLES` in `t12/model.py` |
| Worker tools | `TOOLS` in `t12/tools.py`, keyed by role name |
| Prompts and skills | `prompts/<role>.md` and `skills/<role>/` in the factory checkout |
| Task intake | hand-written `task.json` only; every task repeats gates, sandbox, `verify_deps` |
| Delivery | `ADAPTERS` in `t12/delivery.py`; GitHub and Asana scripts only |

chat-bridge works around this with its own in-repo factory
(`chat-bridge/factory/`), which hardcodes `PROTECTED_PATHS`, its
gate ladder, and its lanes. turbo-12f has no protected-path check
at all, so it cannot yet enforce the rule that `guards/`, `e2e/`,
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

- **The profile is read from the base commit, not the worktree.**
  `prepare` runs `git show <base>:.t12/project.json`, validates it,
  stores it as evidence, and freezes it in the run record. A builder
  that edits `.t12/` in its worktree changes nothing for its own
  run. `green` refuses if the frozen profile hash changed, the same
  way it treats the task file today.
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

All kinds may call `escalate`. `TOOLS` is keyed by kind instead of
role, and `SESSION_ROLES` / `REVIEW_ROLES` are derived from the
frozen profile. The one-session-per-role rule is unchanged. A
project adds a reviewer (UX copy, accessibility, data privacy) by
adding a `review` role with its own prompt. It needs no change to
the core.

### Prompts and skills resolve in layers

`t12 context --role R` builds the prompt from:

1. The role's prompt: `.t12/prompts/<file>` at the base commit if
   the profile names one, else the factory's `prompts/<kind>.md`.
2. Each skill the role lists: `.t12/skills/<name>/SKILL.md` at the
   base commit, else the factory's `skills/<name>/SKILL.md`.
3. `context_docs`, as paths the worker must read. They are listed,
   not inlined, so the context stays small (Factor 3).

The context JSON records the blob hash of every prompt and skill it
used, so a review can tell which instructions a worker saw.

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

## Workflow: configurable inside fixed invariants

The phase machine (`ready, red, gates, green, local-verified`) does
not become configurable. It is the reason a `local-verified` run
means anything. A project configures what runs inside it:

- the gate ladder, its order, and its commands
- which review roles green requires, including new ones
- the sandbox adapter and image
- `red: "waived"` for a task with no behaviour to test, such as a
  docs-only change. The task must carry a reason, the run records
  the waiver, and the manifest shows it. The default is `required`.

A project cannot remove replay, the re-hash at green, the review
void on re-stage, or the uncertain-delivery block.

## Plan

Each step is one PR on top of the last. Each ends in a check that
runs against a disposable repo, as the current tests do.

1. **Project profile.** Read and freeze `.t12/project.json` from
   base. Task overrides on top. Protected-path check at `stage`.
   `t12 init --scaffold` writes a starter profile. The check is a
   test where the builder edits `.t12/project.json` and a protected
   path, and the run uses the base profile and refuses the stage.
2. **Roles and skills.** Role-to-kind map, `TOOLS` keyed by kind,
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
