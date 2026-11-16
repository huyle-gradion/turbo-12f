# Planner: from client specs to Asana sub-tasks

Status: proposal, decisions taken. Nothing here is built.

The Planner is the block before intake. People (PM, BA and tech lead,
together "PBT") and an agent turn a client's specs into Asana sub-tasks
the factory can deliver. It runs as interactive agent sessions with a
person present. The core takes no part: it never calls a model, and its
input stays an approved sub-task read by the `asana-tasks` intake
adapter.

## Decisions

- **Where it runs.** Skills in an interactive session (Claude Code or
  omp). No new runtime and no runner: a person is always present.
- **Where things live.** The planning record is raw files in a directory
  of the product repo. Tasks and sub-tasks live in Asana.
- **The score.** A rubric: four categories of 25 points that add up to
  100. Every deducted point is backed by findings.
- **Who approves.** Anyone on the PBT team or a developer may add the
  approval tag to a sub-task.

## The rubric

| Category (25 each) | Question |
|---|---|
| Completeness | Is anything needed missing: users, flows, errors, data, limits? |
| Consistency | Do parts contradict each other or the client's other documents? |
| Testability | Could a local test check each requirement? |
| Ambiguity | Could two engineers build it differently? |

Weights sit in the versioned prompt file, so a project can change them.
The total answers "rate it out of 100"; the categories show PBT what
moved between rounds.

## The planning record

One directory per story in the product repo, for example
`planning/<story>/`:

| Path | Holds | Written by |
|---|---|---|
| `specs/v<N>/` | the client's files, raw, one folder per version | PBT |
| `evaluations/round-<N>.json` | scores and findings | the evaluate skill |
| `judgements/round-<N>.json` | PBT's verdict on each finding | PBT, through the skill |
| `questions/round-<N>.md` | questions for the client | rendered from the judgements |
| `decisions.md` | answers from grilling | the grill skill |
| `story.md`, `subtasks/*.md` | the Asana task and sub-task bodies | the split skill |
| `asana.json` | Asana ids of the task and sub-tasks | the export adapter |

The profile lists `planning/` in `protected_paths`, so no factory worker
can change the record it is building from.

## Steps

1. **Client specs.** The client's documents are kept as given, so every
   later version can be compared with the original.
2. **Evaluate.** The agent acts as a senior product manager: it
   evaluates the requirements, lists the discrepancies and rates them
   out of 100. PBT judges each finding (correct, evaluate again, or ask
   the client). The questions for the client come from the findings PBT
   kept. The loop repeats until PBT is satisfied.
3. **Grill.** PBT and the agent run `grill-me` or `grill-with-docs`
   (Matt Pocock's skills) on the evaluated specs until the agent has no
   knowledge gap left or PBT is satisfied. Each answer becomes a
   decision.
4. **Asana Task.** The result is exported as one Asana task: the story,
   with its summary, decisions and what is out of scope. PBT and the
   developers review it. It has sub-tasks, so intake never runs it.
5. **Sub-tasks.** The reviewed task is split into sub-tasks, each one
   unit of factory work: given/when/then criteria, a `Touches:` line, at
   most 10 criteria, one test file. PBT adds the approval tag to each
   sub-task; `asana-tasks` then queues it.

## How it follows 12-Factor Agents

| Factor | In the Planner |
|---|---|
| 1, 4: tools are structured outputs | The evaluation is a list of findings, each quoting the spec line it is about, with a kind, a severity and a question for the client, so PBT can judge each one. |
| 2: own your prompts | The senior-PM prompt is a versioned file, pinned like the factory's other prompts; grill skills are pinned by commit. |
| 3: own your context window | Each session gets the original specs, the kept findings and the decisions so far, not the whole history of earlier rounds. |
| 5, 12: state in records | Spec versions, evaluations, PBT's judgements and decisions are kept in the planning record, so a round can be replayed or audited. |
| 6, 7: pause and contact humans | Waiting days for the client is a pause, not a failure. Both loops end only when a person says so. |
| 10: small, focused agents | Evaluate, grill and split are separate sessions with separate prompts. |

## What already exists

- `adapters/asana-tasks.py`: queues approved open sub-tasks and never
  runs a task that has open sub-tasks. The approval marker is a tag.
- `adapters/common.py spec-check --forge asana`: the same rules intake
  applies, run before a sub-task is created.
- `factory-spec`: the skill that writes one deliverable issue; step 5
  follows its rules.

## Limits

- The score out of 100 is a model's judgement, not a measurement: the
  same specs can score differently in two runs. It is a signal between
  rounds for PBT, never a gate.
- For an empty product, `Touches:` names files that do not exist yet;
  spec-check allows that and lists them.
