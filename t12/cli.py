"""turbo-12f CLI. Each command is one deterministic step: load the run
record from disk, validate, act, persist atomically. The LLM never
runs here; `context` prints the owned prompt + minimal context for
the operator to feed to a model, and the other commands verify what
the worker did in the worktree.
"""

import argparse
import json
import os
import re
import shlex
import sys
import time

from . import delivery as delivery_mod
from . import evidence, gates as gate_mod, gitio, instructions, limits, model, retro, runner, sandbox, store, tools
from . import profile as profile_mod
from .errors import StateError, T12Error

VERSION = "0.1.0"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- helpers

def _home(args):
    return os.path.abspath(args.home)


def _config(home):
    path = os.path.join(home, "config.json")
    if not os.path.exists(path):
        raise StateError(f"no turbo-12f state home at {home}; run `init` first")
    return store.load_json(path)


def _run(home, task_id):
    return model.load_run(home, task_id)


def _save(home, run, event_type, detail=None):
    model.save_run(home, run)
    model.append_event(home, run, event_type, detail)


def _check_worktree(run):
    wt = run["worktree"]
    if not gitio.is_git_repo(wt):
        raise StateError(f"worktree missing or not a git repo: {wt}")
    if gitio.current_branch(wt) != run["branch"]:
        raise StateError(
            f"worktree is on {gitio.current_branch(wt)!r}, expected {run['branch']!r}"
        )
    return wt


def _check_candidate_tree(run, action):
    wt = _check_worktree(run)
    if not gitio.is_clean(wt):
        raise StateError(f"refusing {action}: worktree has uncommitted changes")
    if gitio.head(wt) != run["candidate"]["commit"]:
        raise StateError(f"refusing {action}: HEAD moved since stage")
    return wt


def _time_left(run):
    left = int(model.time_left_s(run))
    if left <= 0:
        raise StateError("time budget exhausted")
    return left


def _verification(home, run, worktree):
    """Environment + sandbox context for verification commands. The
    context carries the run's adapter, so every verification command
    uses the adapter the run was admitted with."""
    env = sandbox.verification_env(home, run["task_id"])
    ctx = {"worktree": worktree, "verify_home": env["HOME"],
           "adapter": run.get("sandbox_adapter"),
           "image": run.get("sandbox_image")}
    return env, ctx


def _run_on_commit(home, run, wt, commit, argv):
    """Run a verification command in a fresh checkout of `commit`, with
    the task's declared dependency folders linked in read-only."""
    path = model.verify_checkout_dir(home, run["task_id"])
    with gitio.detached_checkout(wt, path, commit) as checkout:
        env, ctx = _verification(home, run, checkout)
        ctx["read_only"] = _link_deps(run, wt, checkout)
        obs = runner.run(argv, cwd=checkout, timeout_s=_time_left(run),
                         env=env, sandbox=ctx)
    obs["deps"] = list(run.get("verify_deps") or [])
    return obs


def _link_deps(run, wt, checkout):
    real = []
    for dep in run.get("verify_deps") or []:
        source = os.path.join(wt, dep)
        target = os.path.join(checkout, dep)
        if not os.path.exists(source):
            raise StateError(f"declared dependency missing in the worktree: {dep}")
        if os.path.lexists(target):
            raise StateError(f"declared dependency {dep} is tracked in the commit; "
                             "verify_deps must name untracked paths")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        expected = os.path.join(os.path.realpath(checkout), os.path.dirname(os.path.normpath(dep)))
        if os.path.realpath(os.path.dirname(target)) != os.path.normpath(expected):
            raise StateError(f"declared dependency {dep} sits under a symlink in the commit; "
                             "verify_deps must name plain paths")
        real.append(os.path.realpath(source))
        os.symlink(real[-1], target)
    return real


def _require_probes(run):
    if run.get("require_probes") and not sandbox.probes_passed(run.get("probes") or []):
        raise StateError(
            "safety probes have not passed for this run; "
            "run `probe` and fix the environment first"
        )


def _verify_chain_or_raise(home, run):
    problems = model.verify_chain(home, run)
    if problems:
        raise StateError("event log failed verification: " + "; ".join(problems[:3]))


def _last_compacted_error(home, run):
    """Factor 9: the next worker gets a compacted error, not raw logs."""
    path = model.events_path(home, run["task_id"])
    if not os.path.exists(path):
        return None
    last = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in ("gate-failed", "replay-failed", "red-failed"):
                last = event
    if not last:
        return None
    detail = last.get("detail", {})
    output = detail.get("output", "")
    lines = output.splitlines()[-40:]
    return {
        "event": last["type"],
        "step": detail.get("step"),
        "exit_code": detail.get("exit_code"),
        "output_tail": lines,
    }


def _manifest_text(run):
    lines = [
        f"turbo-12f manifest — task {run['task_id']}: {run['title']}",
        f"phase: {run['phase']}",
        f"repo: {run['repo']}",
        f"base: {run['base']}  branch: {run['branch']}",
        f"config_digest: {run['config_digest']}",
        "",
        "Acceptance criteria:",
    ]
    for criterion in run["acceptance_criteria"]:
        lines.append(f"  - {criterion}")
    lines.append("")
    if run["red"]:
        r = run["red"]
        lines += [
            f"red commit: {r['commit']}  test: {r['test_path']}  blob: {r['test_blob'][:12]}",
            f"red command: {' '.join(r['command'])}  exit={r['exit_code']}",
            f"red output: {r['output']['name']} sha256={r['output']['sha256'][:16]}",
            "",
        ]
    if run["candidate"]:
        lines.append(f"candidate commit: {run['candidate']['commit']}")
        lines.append("")
    lines.append("Gates:")
    for gate in run["gates"]:
        obs = gate["observation"]
        extra = ""
        if obs:
            extra = f" exit={obs['observation']['exit_code']} sha256={obs['sha256'][:16]}"
        lines.append(f"  - {gate['name']}: {gate['status']}{extra}")
    lines.append("")
    if run["replay"]:
        rp = run["replay"]
        lines += [
            f"replay: exit={rp['exit_code']} on {rp['commit'][:12]}",
            f"replay output: {rp['output']['name']} sha256={rp['output']['sha256'][:16]}",
            "",
        ]
    b = run["budgets"]
    lines.append("")
    lines.append(f"Sandbox adapter: {run.get('sandbox_adapter') or 'unknown'}"
                 + (f" (image {run['sandbox_image']})" if run.get("sandbox_image") else ""))
    lines.append("Probes:")
    for probe in run.get("probes") or []:
        lines.append(f"  - {probe['name']}: {'passed' if probe['passed'] else 'FAILED'} — {probe['detail']}")
    if not run.get("probes"):
        lines.append("  - (none recorded)")
    lines.append(
        f"budgets: time {b['time_limit_s']}s, ops {b['ops_used']}/{b['op_limit']}, "
        f"repairs {b['repairs_used']}/{b['repair_cap']}"
    )
    if run.get("measurements"):
        m = run["measurements"]
        lines.append(
            f"measured: wall {m['wall_used_s']}s, ops {m['ops_used']}, "
            f"repairs {m['repairs_used']}, gates {m['gates']}"
        )
    if run["stop_reason"]:
        lines.append(f"stop reason: {run['stop_reason']}")
    lines.append("")
    lines.append(
        "local-verified means the configured evidence chain was observed "
        "and is internally consistent. It is not acceptance and not "
        "permission to merge."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- commands

def cmd_init(args):
    repo = os.path.abspath(args.repo)
    home = os.path.abspath(args.home)
    if not gitio.is_git_repo(repo):
        raise StateError(f"not a git repository: {repo}")
    if os.path.commonpath([home, repo]) in (home, repo):
        raise StateError("state home must live outside the product repo")
    for sub in ("", "runs", "evidence", "worktrees"):
        store.mkdir_private(os.path.join(home, sub))
    config_path = os.path.join(home, "config.json")
    gitio.rev_parse(repo, f"refs/heads/{args.trunk}")
    config = {"version": model.VERSION, "repo": repo, "trunk": args.trunk,
              "created_at": time.time()}
    if os.path.exists(config_path):
        existing = store.load_json(config_path)
        if existing.get("repo") != repo:
            raise StateError(
                f"state home already bound to {existing.get('repo')}, not {repo}"
            )
        if "trunk" in existing and existing["trunk"] != args.trunk:
            raise StateError(
                f"state home already follows trunk {existing['trunk']!r}, "
                f"not {args.trunk!r}; use a new state home to change it"
            )
    store.save_json(config_path, config)
    print(f"init: repo={repo} trunk={args.trunk} home={home}")
    return 0


def cmd_prepare(args):
    home = _home(args)
    config = _config(home)
    task = store.load_json(args.task)
    task_id = task.get("id")
    with model.run_lock(home, task_id):
        return _admit(home, config["repo"], config.get("trunk", "main"), task, task_id,
                      args.supersede)


def _admit(home, repo, trunk, task, task_id, supersede):
    if not gitio.is_clean(repo):
        raise StateError(f"product repo has uncommitted changes: {repo}")
    base = task.get("base", "main")
    base_commit = gitio.rev_parse(repo, base)
    # The rules come from the trunk the state home names, never from the
    # task's base: a task file could point base at a commit, or another
    # run's branch, whose profile is weaker or missing.
    trunk_commit = gitio.rev_parse(repo, f"refs/heads/{trunk}")
    if not gitio.git_ok_bool(repo, "merge-base", trunk_commit, base_commit):
        raise StateError(f"base {base!r} shares no history with trunk {trunk!r}")
    if gitio.file_at(repo, trunk_commit, profile_mod.LEGACY_PATH) is not None:
        raise StateError(f"{profile_mod.LEGACY_PATH} is no longer read; move its "
                         f"security_review_paths into {profile_mod.PATH} on {trunk}")
    profile = profile_mod.parse(gitio.file_at(repo, trunk_commit, profile_mod.PATH))
    task = profile_mod.apply(profile, task)
    for role in profile["roles"]:
        instructions.resolve(repo, trunk_commit, profile["roles"], role, PROJECT_ROOT)
    for gate in task.get("gates", []):
        try:
            sandbox.check_command(gate.get("command") or [])
        except T12Error as exc:
            raise StateError(
                f"gate {gate.get('name')!r} rejected at admission: {exc}"
            ) from exc
    supersedes = None
    if os.path.exists(model.run_path(home, task_id)):
        if not supersede:
            raise StateError(
                f"run already exists for task {task_id!r}; refusing to overwrite — "
                "stop it, then re-run prepare with --supersede"
            )
        old = model.load_run(home, task_id)
        if old["phase"] != "stopped":
            raise StateError(
                f"cannot supersede: run is {old['phase']!r}; stop it first"
            )
        supersedes = model.archive_run(home, old)
        # The old worktree and factory branch are disposable working
        # state; the archived run record keeps every SHA and ref.
        gitio.remove_worktree_force(repo, old["worktree"])
        if gitio.branch_exists(repo, old["branch"]):
            gitio.delete_branch(repo, old["branch"])
    calibrated = limits.load_limits(home)
    adapter, image = sandbox.resolve_adapter(task)
    run = model.new_run(task, repo, home, base_commit, profile["roles"], supersedes=supersedes,
                        defaults=calibrated,
                        defaults_source="calibrated" if calibrated else "builtin",
                        sandbox_adapter=adapter, sandbox_image=image)
    for key in profile_mod.SCOPE_KEYS:
        run[key] = profile[key]
    run["trunk"] = trunk
    run["trunk_commit"] = trunk_commit
    if gitio.branch_exists(repo, run["branch"]):
        raise StateError(f"branch already exists: {run['branch']}")
    gitio.add_worktree(repo, run["worktree"], run["branch"], base_commit)
    model.transition(run, "ready")
    _save(home, run, "prepared", {"branch": run["branch"], "base": base,
                                  "supersedes": supersedes})
    probes = sandbox.run_probes(home, run, runner.run)
    run["probes"] = probes
    if run["require_probes"] and not sandbox.probes_passed(probes):
        run["stop_reason"] = "probes-failed"
        model.transition(run, "stopped")
        _save(home, run, "stopped", {"reason": "probes-failed"})
        raise StateError(
            "safety probes failed and the task requires them; run stopped — "
            "see probes.json in the run evidence"
        )
    _save(home, run, "probed", {"probes": [
        {"name": p["name"], "passed": p["passed"]} for p in probes]})
    if not sandbox.probes_passed(probes):
        print(f"warning: safety probes failed (adapter={run['sandbox_adapter']}); "
              "see probes.json — isolation is NOT enforced for this run")
    print(f"ready: task={task_id} worktree={run['worktree']} branch={run['branch']}")
    return 0


def cmd_context(args):
    home = _home(args)
    run = _run(home, args.task_id)
    kind = model.role_kind(run, args.role)
    used = instructions.resolve(run["repo"], run["trunk_commit"], run["role_map"],
                                args.role, PROJECT_ROOT)
    wt = run["worktree"]
    changed = gitio.dirty_paths(wt) if os.path.isdir(wt) else []
    prior_review = None
    if kind == "review":
        for review in reversed(run.get("reviews") or []):
            if review["role"] == args.role:
                prior_review = {
                    "verdict": review["verdict"],
                    "commit": review["commit"],
                    "report": review["report"]["name"],
                    "voided": review["voided"],
                }
                break
    elif kind == "builder":
        review = model.changes_requested(run)
        if review is not None:
            with open(review["report"]["path"], encoding="utf-8", errors="replace") as fh:
                findings = fh.read()[-4000:]
            prior_review = {
                "role": review["role"],
                "verdict": review["verdict"],
                "commit": review["commit"],
                "report": review["report"]["name"],
                "report_path": review["report"]["path"],
                "findings": findings,
            }
    context = {
        "task_id": run["task_id"],
        "title": run["title"],
        "acceptance_criteria": run["acceptance_criteria"],
        "phase": run["phase"],
        "requested_phase": args.phase,
        "branch": run["branch"],
        "worktree": wt,
        "base": run["base"],
        "touches": run.get("touches"),
        "protected_paths": run.get("protected_paths", []),
        "budgets": {
            "time_left_s": int(model.time_left_s(run)),
            "ops_left": run["budgets"]["op_limit"] - run["budgets"]["ops_used"],
            "repairs_left": run["budgets"]["repair_cap"] - run["budgets"]["repairs_used"],
        },
        "gates": [{ "name": g["name"], "status": g["status"]} for g in run["gates"]],
        "roles": run.get("roles", {}),
        "prior_review": prior_review,
        "covers": run["red"].get("covers") if run["red"] else None,
        "red_test": {k: run["red"][k] for k in ("test_path", "command", "marker", "commit")}
        if run["red"] else None,
        "changed_files": changed,
        "last_compacted_error": _last_compacted_error(home, run),
        "human_responses": [
            {"question": e["question"], "answer": e["answer"]}
            for e in run.get("escalations") or [] if e["answer"] is not None
        ],
        "tools": tools.schema_for(kind),
        "instructions": [{k: entry[k] for k in ("source", "path", "sha256")} for entry in used],
    }
    prompt, *skills = used
    print(prompt["text"])
    for skill in skills:
        print(f"--- skill: {skill['path']} ({skill['source']}) ---")
        print(skill["text"])
    print("--- context (JSON) ---")
    print(json.dumps(context, indent=2, sort_keys=True))
    return 0


def _parse_covers(raw, criteria_count):
    """The test-writer's map from each acceptance criterion index to the
    tests that check it. Every criterion must be named."""
    try:
        covers = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise StateError(f"cannot parse --covers as JSON: {exc}") from exc
    if not isinstance(covers, dict):
        raise StateError("--covers must map criterion indices to lists of test names")
    indices = [str(i) for i in range(criteria_count)]
    unknown = sorted(k for k in covers if k not in indices)
    if unknown:
        raise StateError(f"refusing red: covers names unknown criterion indices {unknown}; "
                         f"criteria are {indices}")
    missing = [i for i in indices if i not in covers]
    if missing:
        raise StateError(f"refusing red: covers is missing criterion indices {missing}")
    for index, tests in covers.items():
        if (not isinstance(tests, list) or not tests
                or not all(isinstance(t, str) and t.strip() for t in tests)):
            raise StateError(f"refusing red: covers[{index!r}] must be a non-empty list of test names")
    return covers


def _named(name, text):
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None


def _short_test_name(name):
    return re.split(r"::|\.", name)[-1]


_FAILURE_WORD = re.compile(r"(?<!\w)(?:FAIL|FAILED|ERROR|not ok)(?!\w)")


def _defines_test(name, source):
    """True when the source defines a function with the test's short name."""
    short = re.escape(_short_test_name(name))
    return re.search(rf"^\s*(?:async\s+)?def\s+{short}\s*\(", source, re.M) is not None


def _names_test(name, line):
    """True when the line names the test. A qualified name (Class.method or
    path::Class::method) must appear qualified, in either separator style,
    so a test cannot borrow the failure of a same-named method elsewhere."""
    parts = [re.escape(p) for p in re.split(r"::|\.", name) if p]
    if len(parts) == 1:
        return _named(name, line)
    joined = r"(?:\.|::)".join(parts)
    return re.search(rf"(?<![\w]){joined}(?!\w)", line) is not None


def _reported_failing(name, output):
    """True when a line of the runner's output names the test and marks it failed."""
    return any(_names_test(name, line) and _FAILURE_WORD.search(line)
               for line in output.splitlines())


def cmd_red(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "ready":
        raise StateError(f"red requires phase 'ready', run is {run['phase']!r}")
    model.check_budgets(run)
    _require_probes(run)
    model.check_session(run, args.role, args.session)
    wt = _check_worktree(run)
    test_path = args.test_path
    if not os.path.exists(os.path.join(wt, test_path)):
        raise StateError(f"test file not found in worktree: {test_path}")
    if model.outside_touches(run, [os.path.normpath(test_path)]):
        raise StateError(f"refusing red: the test is outside the task's touches: {test_path}")
    dirty = set(gitio.dirty_paths(wt))
    if dirty - {test_path}:
        raise StateError(f"refusing red: unrelated dirty files: {sorted(dirty - {test_path})}")
    if test_path not in dirty:
        raise StateError(f"refusing red: {test_path} has no changes to commit")
    try:
        command = shlex.split(args.command)
    except ValueError as exc:
        raise StateError(f"cannot parse --command: {exc}") from exc
    if not command:
        raise StateError("--command must not be empty")
    covers = _parse_covers(args.covers, len(run["acceptance_criteria"]))
    covered = sorted({t for tests in covers.values() for t in tests})
    commit = gitio.commit_with_path(wt, test_path, f"test({run['task_id']}): red test")
    test_source = gitio.file_at(wt, commit, test_path)
    absent = [t for t in covered if not _defines_test(t, test_source)]
    if absent:
        raise StateError(f"refusing red: covers names tests not in the red commit's {test_path}: {absent}")
    obs = _run_on_commit(home, run, wt, commit, command)
    model.bump_op(run)
    if obs["timed_out"] or obs["exit_code"] == 0:
        _save(home, run, "red-failed", {
            "step": "red", "exit_code": obs["exit_code"],
            "output": (obs["stdout"] + obs["stderr"])[-4000:],
        })
        raise StateError(
            "red test did not fail as required "
            f"(exit={obs['exit_code']}, timed_out={obs['timed_out']}); "
            "a passing test cannot be the red test"
        )
    combined = obs["stdout"] + obs["stderr"]
    if args.marker not in combined:
        _save(home, run, "red-failed", {
            "step": "red", "exit_code": obs["exit_code"],
            "output": combined[-4000:],
        })
        raise StateError(f"red output lacks the expected failure marker: {args.marker!r}")
    passing = [t for t in covered if not _reported_failing(t, combined)]
    if passing:
        _save(home, run, "red-failed", {
            "step": "red", "exit_code": obs["exit_code"],
            "output": combined[-4000:],
        })
        raise StateError(f"refusing red: covered tests do not fail at the red commit: {passing}")
    ref = evidence.write_observation(home, run["task_id"], "red-output.txt", obs)
    gitio.move_branch_to(wt, commit)
    run["red"] = {
        "test_path": test_path,
        "command": command,
        "marker": args.marker,
        "exit_code": obs["exit_code"],
        "output": ref,
        "test_blob": gitio.blob_at(wt, commit, test_path),
        "commit": commit,
        "covers": covers,
    }
    model.bind_session(run, args.role, args.session)
    model.transition(run, "red")
    _save(home, run, "red", {"commit": commit, "test_path": test_path})
    print(f"red: commit={commit[:12]} test={test_path}")
    return 0


def cmd_stage(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] not in ("red", "gates", "green"):
        raise StateError(f"stage requires phase 'red', 'gates' or 'green', run is {run['phase']!r}")
    model.check_budgets(run)
    model.check_session(run, args.role, args.session)
    wt = _check_worktree(run)
    if not gitio.is_clean(wt):
        raise StateError("refusing stage: worktree has uncommitted changes")
    candidate = gitio.head(wt)
    review = model.changes_requested(run)
    if review is not None and candidate == review["commit"]:
        model.bump_op(run)
        _save(home, run, "stage-refused", {
            "reason": "no new commit after changes requested",
            "review": review["report"]["name"], "commit": candidate[:12],
        })
        raise StateError(
            f"refusing stage: the {review['role']} review ({review['report']['name']}) "
            f"asked for changes on {candidate[:12]} and no new commit was made — "
            "address its findings, commit, then stage"
        )
    if candidate == run["red"]["commit"]:
        raise StateError("refusing stage: no implementation commit after the red commit")
    if not gitio.is_ancestor(wt, run["red"]["commit"], candidate):
        raise StateError("refusing stage: candidate does not descend from the red commit")
    if gitio.blob_hash(wt, run["red"]["test_path"]) != run["red"]["test_blob"]:
        raise StateError("refusing stage: red test file changed after the red phase")
    sensitive = []
    if run.get("base_commit") and "trunk_commit" not in run:
        raise StateError("refusing stage: this run was prepared before project profiles; "
                         "stop it and prepare it again with --supersede")
    if run.get("base_commit"):
        changed = gitio.changed_paths(wt, run["base_commit"], candidate)
        outside = model.outside_touches(run, changed)
        if outside:
            raise StateError("refusing stage: the candidate changes paths outside the "
                             f"task's touches: {', '.join(outside)}")
        # Measured from where the candidate leaves trunk, so a base that
        # already carries an unreviewed edit cannot hide it.
        fork = gitio.merge_base(wt, run["trunk_commit"], candidate)
        changed = sorted(set(changed) | set(gitio.changed_paths(wt, fork, candidate)))
        protected = [p for p in changed if model.under(p, run["protected_paths"])]
        if protected:
            raise StateError("refusing stage: the candidate changes protected paths: "
                             f"{', '.join(protected)}")
        roots = profile_mod.security_roots(run)
        sensitive = [p for p in changed if model.under(p, roots)]
        if sensitive and "security" not in run["required_reviews"]:
            run["required_reviews"].append("security")
    if run["phase"] in ("gates", "green"):
        gate_mod.reset_for_new_candidate(run)
        run["replay"] = None
        model.void_reviews(run, f"superseded by {candidate[:12]}")
    run["candidate"] = {"commit": candidate, "staged_at": time.time()}
    model.bump_op(run)
    model.bind_session(run, args.role, args.session)
    model.transition(run, "gates")
    _save(home, run, "staged", {"commit": candidate, "security_paths": sensitive})
    print(f"staged: candidate={candidate[:12]}")
    return 0


def cmd_gate(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "gates":
        raise StateError(f"gate requires phase 'gates', run is {run['phase']!r}")
    model.check_budgets(run)
    _require_probes(run)
    if model.role_kind(run, args.role) != "qa":
        raise StateError(f"gates run under a qa-kind role; {args.role!r} is not one")
    model.check_session(run, args.role, args.session)
    wt = _check_candidate_tree(run, "gate")
    gate = gate_mod.find_gate(run, args.name)
    gate_mod.check_order(run, gate)
    obs = _run_on_commit(home, run, wt, run["candidate"]["commit"], gate["command"])
    ref = evidence.write_observation(home, run["task_id"], f"gate-{gate['name']}-output.txt", obs)
    gate["run_by"] = args.session
    gate["run_by_role"] = args.role
    model.bind_session(run, args.role, args.session)
    gate["commit"] = run["candidate"]["commit"]
    model.bump_op(run)
    if obs["timed_out"] or obs["exit_code"] != 0:
        gate["status"] = "failed"
        gate["observation"] = ref
        run["budgets"]["repairs_used"] += 1
        _save(home, run, "gate-failed", {
            "step": gate["name"], "exit_code": obs["exit_code"],
            "duration_s": obs["duration_s"],
            "output": (obs["stdout"] + obs["stderr"])[-4000:],
        })
        if run["budgets"]["repairs_used"] > run["budgets"]["repair_cap"]:
            run["phase"] = "stopped"
            run["stop_reason"] = "repair-cap"
            model.record_measurements(run)
            _save(home, run, "stopped", {"reason": "repair-cap"})
            raise StateError(
                f"gate {gate['name']!r} failed and the repair cap "
                f"({run['budgets']['repair_cap']}) is exceeded; run stopped"
            )
        raise StateError(
            f"gate {gate['name']!r} failed (exit={obs['exit_code']}); "
            f"repairs used {run['budgets']['repairs_used']}/{run['budgets']['repair_cap']} — "
            "fix in the worktree and re-stage"
        )
    gate["status"] = "passed"
    gate["observation"] = ref
    _save(home, run, "gate-passed", {"gate": gate["name"], "duration_s": obs["duration_s"]})
    if gate_mod.all_passed(run):
        print(f"gate {gate['name']}: passed — all gates passed, run replay next")
    else:
        nxt = gate_mod.next_gate(run)
        print(f"gate {gate['name']}: passed — next gate: {nxt['name']}")
    return 0


def cmd_replay(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "gates":
        raise StateError(f"replay requires phase 'gates', run is {run['phase']!r}")
    if not gate_mod.all_passed(run):
        raise StateError("replay requires all gates to pass first")
    model.check_budgets(run)
    _require_probes(run)
    wt = _check_candidate_tree(run, "replay")
    obs = _run_on_commit(home, run, wt, run["candidate"]["commit"], run["red"]["command"])
    model.bump_op(run)
    if obs["timed_out"] or obs["exit_code"] != 0:
        _save(home, run, "replay-failed", {
            "step": "replay", "exit_code": obs["exit_code"],
            "output": (obs["stdout"] + obs["stderr"])[-4000:],
        })
        raise StateError(
            f"replay failed (exit={obs['exit_code']}): the red test does not "
            "pass on the candidate — fix in the worktree and re-stage"
        )
    ref = evidence.write_observation(home, run["task_id"], "replay-output.txt", obs)
    run["replay"] = {
        "command": run["red"]["command"],
        "exit_code": obs["exit_code"],
        "output": ref,
        "commit": run["candidate"]["commit"],
    }
    model.transition(run, "green")
    _save(home, run, "replayed", {"commit": run["candidate"]["commit"]})
    print(f"replay: passed on {run['candidate']['commit'][:12]} — run green next")
    return 0


def cmd_review(args):
    home = _home(args)
    run = _run(home, args.task_id)
    role = args.role
    kind = model.role_kind(run, role)
    if kind != "review":
        raise StateError(f"{role!r} is a {kind} role; only review roles record reviews")
    if run["phase"] not in ("gates", "green"):
        raise StateError(f"review requires phase 'gates' or 'green', run is {run['phase']!r}")
    if not gate_mod.all_passed(run):
        raise StateError("review requires all gates to pass first")
    model.check_budgets(run)
    _require_probes(run)
    model.check_session(run, role, args.session)
    wt = _check_worktree(run)
    if gitio.head(wt) != run["candidate"]["commit"]:
        raise StateError("refusing review: HEAD moved since stage")
    report_path = os.path.abspath(args.report)
    if not os.path.isfile(report_path):
        raise StateError(f"review report not found: {args.report}")
    with open(report_path, "rb") as fh:
        data = fh.read()
    if not data.strip():
        raise StateError("review report is empty")
    prior = None
    for review in reversed(run.get("reviews") or []):
        if review["role"] == role:
            prior = review
            break
    n = len([r for r in run.get("reviews") or [] if r["role"] == role]) + 1
    ref = evidence.write_evidence(home, run["task_id"], f"review-{role}-{n}.md", data)
    model.bind_session(run, role, args.session)
    model.bump_op(run)
    run["reviews"].append({
        "role": role,
        "session": args.session,
        "verdict": args.verdict,
        "commit": run["candidate"]["commit"],
        "report": ref,
        "prior_review": prior["report"]["name"] if prior else None,
        "voided": False,
        "voided_reason": None,
        "at": time.time(),
    })
    # Each changes-requested round is one repair, so a builder that
    # cannot satisfy the reviewer (or a finding the builder does not
    # own) stops at the cap instead of looping review forever.
    if args.verdict == "changes-requested":
        run["budgets"]["repairs_used"] += 1
    _save(home, run, "reviewed", {
        "role": role, "verdict": args.verdict,
        "commit": run["candidate"]["commit"][:12],
    })
    if args.verdict == "changes-requested":
        if run["budgets"]["repairs_used"] > run["budgets"]["repair_cap"]:
            run["phase"] = "stopped"
            run["stop_reason"] = "repair-cap"
            model.record_measurements(run)
            _save(home, run, "stopped", {"reason": "repair-cap", "review": ref["name"]})
            raise StateError(
                f"{role} requested changes ({ref['name']}) and the repair cap "
                f"({run['budgets']['repair_cap']}) is exceeded; run stopped — "
                f"the unresolved findings are in {ref['name']}"
            )
    print(f"review: {role} {args.verdict} on {run['candidate']['commit'][:12]}")
    return 0


def cmd_deliver(args):
    """Record an adapter's delivery report as evidence. The core
    never touches the network: the adapter did the remote work and
    wrote the report; this command checks the report's shape, pins
    it to the reviewed head, and enforces the uncertain block."""
    home = _home(args)
    run = _run(home, args.task_id)
    if args.adapter not in delivery_mod.ADAPTERS:
        raise StateError(f"unknown adapter {args.adapter!r}; adapters are {list(delivery_mod.ADAPTERS)}")
    delivery_mod.check_allowed(run, args.adapter, args.resolves)
    _verify_chain_or_raise(home, run)
    wt = _check_worktree(run)
    if gitio.head(wt) != run["candidate"]["commit"]:
        raise StateError("refusing deliver: HEAD moved since stage")
    report_path = os.path.abspath(args.report)
    if not os.path.isfile(report_path):
        raise StateError(f"delivery report not found: {args.report}")
    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        raise StateError(f"delivery report is not valid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise StateError("delivery report must be a JSON object")
    if report.get("adapter") != args.adapter:
        raise StateError(
            f"delivery report is for adapter {report.get('adapter')!r}, "
            f"not {args.adapter!r}"
        )
    if report.get("task_id") not in (None, run["task_id"]):
        raise StateError(
            f"delivery report is for task {report.get('task_id')!r}, "
            f"not {run['task_id']!r}"
        )
    with open(report_path, "rb") as fh:
        data = fh.read()
    if not data.strip():
        raise StateError("delivery report is empty")
    n = len([d for d in run.get("deliveries") or [] if d["adapter"] == args.adapter]) + 1
    ref = evidence.write_evidence(
        home, run["task_id"], f"delivery-{args.adapter}-{n}.json", data)
    entry = delivery_mod.record_delivery(run, args.adapter, report, ref)
    _save(home, run, "delivered", {
        "adapter": args.adapter, "outcome": entry["outcome"],
        "head": entry["head"][:12] if entry["head"] else None,
        "ref": entry["ref"], "resolves": bool(args.resolves),
    })
    print(f"deliver: {args.adapter} {entry['outcome']}"
          + (f" ref={entry['ref']}" if entry["ref"] else ""))
    return 0


def cmd_green(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "green":
        raise StateError(f"green requires phase 'green', run is {run['phase']!r}")
    model.check_budgets(run)
    _verify_chain_or_raise(home, run)
    wt = _check_worktree(run)
    if not gitio.is_clean(wt):
        raise StateError("refusing green: worktree has uncommitted changes")
    if gitio.head(wt) != run["candidate"]["commit"]:
        raise StateError("refusing green: HEAD moved since stage")
    if gitio.blob_hash(wt, run["red"]["test_path"]) != run["red"]["test_blob"]:
        raise StateError("refusing green: red test file changed after the red phase")
    evidence.verify_green(home, run)
    model.check_reviews(run)
    model.bump_op(run)
    model.transition(run, "local-verified")
    model.record_measurements(run)
    manifest = _manifest_text(run)
    ref = evidence.write_evidence(home, run["task_id"], "manifest.txt", manifest.encode())
    run["manifest"] = ref
    _save(home, run, "verified", {"manifest_sha256": ref["sha256"]})
    print(f"local-verified: task={run['task_id']} candidate={run['candidate']['commit'][:12]}")
    return 0


def cmd_manifest(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] not in ("green", "local-verified"):
        raise StateError(
            f"manifest requires phase 'green' or 'local-verified', run is {run['phase']!r}"
        )
    text = _manifest_text(run)
    if args.out:
        out = os.path.abspath(args.out)
        store.atomic_write(out, text.encode())
        _save(home, run, "manifest", {"sha256": store.sha256_bytes(text.encode()), "out": out})
        print(f"manifest written to {args.out}")
    else:
        ref = evidence.write_evidence(home, run["task_id"], "manifest.txt", text.encode())
        _save(home, run, "manifest", {"sha256": ref["sha256"]})
        print(f"manifest written: sha256={ref['sha256'][:16]}")
    return 0


def cmd_verify(args):
    home = _home(args)
    run = _run(home, args.task_id)
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    problems = model.verify_chain(home, run)
    check("event-chain", not problems, "; ".join(problems[:3]))

    refs = []
    if run["red"]:
        refs.append(("red", run["red"]["output"]))
    if run["replay"]:
        refs.append(("replay", run["replay"]["output"]))
    for gate in run["gates"]:
        if gate["observation"]:
            refs.append((f"gate:{gate['name']}", gate["observation"]))
    for probe in run.get("probes") or []:
        if probe.get("output"):
            refs.append((f"probe:{probe['name']}", probe["output"]))
    for review in run.get("reviews") or []:
        refs.append((f"review:{review['role']}", review["report"]))
    for entry in run.get("deliveries") or []:
        refs.append((f"delivery:{entry['adapter']}", entry["report"]))
    ref_problems = []
    for label, ref in refs:
        try:
            evidence.verify_ref(home, run["task_id"], ref)
        except T12Error as exc:
            ref_problems.append(f"{label}: {exc}")
    check("evidence-refs", not ref_problems, "; ".join(ref_problems[:3]))

    exit_problems = []
    if run["red"]:
        obs = run["red"]["output"].get("observation", {})
        if run["red"]["exit_code"] == 0:
            exit_problems.append("red exit_code is 0 (red must fail)")
        if obs.get("exit_code") != run["red"]["exit_code"]:
            exit_problems.append("red exit_code disagrees with its observation")
    if run["replay"]:
        if run["replay"]["exit_code"] != 0:
            exit_problems.append("replay exit_code is not 0")
        if run["candidate"] and run["replay"]["commit"] != run["candidate"]["commit"]:
            exit_problems.append("replay commit is not the candidate")
    for gate in run["gates"]:
        if gate["status"] == "passed":
            obs = (gate["observation"] or {}).get("observation", {})
            if obs.get("exit_code") != 0 or obs.get("timed_out"):
                exit_problems.append(
                    f"gate {gate['name']!r} passed but its observation failed or timed out"
                )
            if run["candidate"] and gate.get("commit") != run["candidate"]["commit"]:
                exit_problems.append(f"gate {gate['name']!r} was not run on the candidate")
    check("exit-codes", not exit_problems, "; ".join(exit_problems[:3]))

    wt_ok, wt_detail = True, ""
    try:
        wt = _check_worktree(run)
        if run["red"] and gitio.blob_hash(wt, run["red"]["test_path"]) != run["red"]["test_blob"]:
            wt_ok, wt_detail = False, "red test file changed"
        phase = run["phase"]
        if phase == "paused":
            phase = run.get("prior_phase")
        expected = None
        if phase == "red":
            expected = run["red"]["commit"]
        elif phase in ("gates", "green", "local-verified"):
            expected = run["candidate"]["commit"]
        elif phase == "ready":
            expected = gitio.rev_parse(_config(home)["repo"], run["base"])
        if expected is not None and gitio.head(wt) != expected:
            wt_ok, wt_detail = False, f"HEAD moved (expected {expected[:12]})"
    except T12Error as exc:
        wt_ok, wt_detail = False, str(exc)
    if run["phase"] == "stopped":
        wt_ok, wt_detail = True, "run stopped, HEAD not pinned"
    check("worktree", wt_ok, wt_detail)

    manifest_ok, manifest_detail = True, "not issued yet"
    try:
        events = model.read_events(home, run["task_id"])
    except T12Error as exc:
        events = []
        manifest_ok, manifest_detail = False, str(exc)
    issued = [e for e in events
              if e.get("type") == "manifest" or
              (e.get("type") == "verified" and e.get("detail", {}).get("manifest_sha256"))]
    if issued:
        last = issued[-1]
        detail = last.get("detail", {})
        if detail.get("out"):
            manifest_detail = f"issued to {detail['out']} (outside state home, not re-hashed)"
        else:
            recorded = detail.get("manifest_sha256") or detail.get("sha256")
            path = os.path.join(home, "evidence", run["task_id"], "manifest.txt")
            if not recorded:
                manifest_ok, manifest_detail = False, "issued event has no sha256"
            elif not os.path.exists(path):
                manifest_ok, manifest_detail = False, "manifest.txt missing"
            elif store.sha256_file(path) != recorded:
                manifest_ok, manifest_detail = False, "manifest.txt changed since issue"
            else:
                manifest_detail = f"matches issued sha256 {recorded[:12]}"
    check("manifest", manifest_ok, manifest_detail)

    for name, ok, detail in checks:
        line = f"{'PASS' if ok else 'FAIL'} {name}"
        if detail and not ok:
            line += f": {detail}"
        elif detail:
            line += f" ({detail})"
        print(line)
    return 0 if all(ok for _, ok, _ in checks) else 1


def cmd_status(args):
    home = _home(args)
    run = _run(home, args.task_id)
    print(json.dumps({
        "task_id": run["task_id"],
        "title": run["title"],
        "phase": run["phase"],
        "branch": run["branch"],
        "worktree": run["worktree"],
        "red_commit": run["red"]["commit"] if run["red"] else None,
        "candidate": run["candidate"]["commit"] if run["candidate"] else None,
        "gates": [{ "name": g["name"], "status": g["status"]} for g in run["gates"]],
        "sandbox_adapter": run.get("sandbox_adapter"),
        "sandbox_image": run.get("sandbox_image"),
        "require_probes": run.get("require_probes", False),
        "probes": [{ "name": p["name"], "passed": p["passed"]}
                   for p in run.get("probes") or []],
        "budgets": {
            "time_left_s": int(model.time_left_s(run)),
            "ops_used": run["budgets"]["ops_used"],
            "op_limit": run["budgets"]["op_limit"],
            "repairs_used": run["budgets"]["repairs_used"],
            "repair_cap": run["budgets"]["repair_cap"],
        },
        "stop_reason": run["stop_reason"],
        "deliveries": run.get("deliveries", []),
    }, indent=2, sort_keys=True))
    return 0


def cmd_pause(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] in ("local-verified", "stopped", "paused"):
        raise StateError(f"cannot pause from phase {run['phase']!r}")
    run["prior_phase"] = run["phase"]
    model.transition(run, "paused")
    _save(home, run, "paused", {"reason": args.reason, "prior_phase": run["prior_phase"]})
    print(f"paused: task={run['task_id']} (was {run['prior_phase']}) — {args.reason}")
    return 0


def cmd_resume(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "paused":
        raise StateError(f"resume requires phase 'paused', run is {run['phase']!r}")
    pending = model.open_escalation(run)
    answer = (args.answer or "").strip()
    if pending and not answer:
        raise StateError(
            f"run is waiting on a human answer to: {pending['question']!r}; "
            "resume with --answer"
        )
    if answer and not pending:
        raise StateError("no open question to answer; resume without --answer")
    prior = run["prior_phase"]
    wt = _check_worktree(run)
    repo = _config(home)["repo"]
    expected = None
    if prior == "ready":
        expected = gitio.rev_parse(repo, run["base"])
    elif prior == "red":
        expected = run["red"]["commit"]
    elif prior in ("gates", "green"):
        expected = run["candidate"]["commit"]
    if expected is not None and gitio.head(wt) != expected:
        raise StateError(
            f"refusing resume: HEAD moved while paused "
            f"(expected {expected[:12]}, found {gitio.head(wt)[:12]})"
        )
    if run["red"] and gitio.blob_hash(wt, run["red"]["test_path"]) != run["red"]["test_blob"]:
        raise StateError("refusing resume: red test file changed while paused")
    # Resume consistency: every recorded evidence ref must still hash
    # to what was recorded. A changed byte while paused fails closed.
    refs = []
    if run["red"]:
        refs.append(run["red"]["output"])
    if run["replay"]:
        refs.append(run["replay"]["output"])
    for gate in run["gates"]:
        if gate["observation"]:
            refs.append(gate["observation"])
    for ref in refs:
        evidence.verify_ref(home, run["task_id"], ref)
    _verify_chain_or_raise(home, run)
    if pending:
        pending["answer"] = answer
        pending["answered_at"] = time.time()
        _save(home, run, "human-response", {"question": pending["question"], "answer": answer})
    model.transition(run, prior)
    run["prior_phase"] = None
    _save(home, run, "resumed", {"phase": prior})
    print(f"resumed: task={run['task_id']} phase={prior}")
    return 0


def cmd_report(args):
    home = _home(args)
    data = limits.collect(home)
    recommendation = limits.recommend(data["completed"])
    if args.json:
        print(json.dumps(
            {"home": home, "report": data, "recommendation": recommendation},
            indent=2, sort_keys=True, default=str))
    else:
        print(limits.report_text(home, data, recommendation), end="")
    return 0


def cmd_board(args):
    home = _home(args)
    _config(home)
    rows = []
    for name in sorted(os.listdir(os.path.join(home, "runs"))):
        if not name.endswith(".json") or ".superseded-" in name:
            continue
        run = model.load_run(home, name[:-len(".json")])
        b = run["budgets"]
        done = run["phase"] in ("local-verified", "stopped")
        rows.append({
            "task_id": run["task_id"],
            "phase": run["phase"],
            "candidate": (run["candidate"] or {}).get("commit"),
            "time_left_s": None if done else int(model.time_left_s(run)),
            "ops_left": None if done else b["op_limit"] - b["ops_used"],
            "repairs_left": None if done else b["repair_cap"] - b["repairs_used"],
            "busy": model.is_busy(home, run["task_id"]),
            "next": model.next_step(run),
        })
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    header = ("TASK", "PHASE", "CANDIDATE", "TIME", "OPS", "REPAIRS", "NEXT")
    table = [header] + [(
        r["task_id"], r["phase"] + (" (busy)" if r["busy"] else ""),
        (r["candidate"] or "-")[:12],
        "-" if r["time_left_s"] is None else f"{r['time_left_s']}s",
        "-" if r["ops_left"] is None else str(r["ops_left"]),
        "-" if r["repairs_left"] is None else str(r["repairs_left"]),
        r["next"]) for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(header) - 1)]
    for row in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[-1])
    return 0


def cmd_retro(args):
    home = _home(args)
    _config(home)
    data = retro.collect(home)
    found = retro.proposals(data, args.min)
    if args.json:
        print(json.dumps(dict(data, home=home, proposals=found), indent=2, sort_keys=True))
    else:
        print(retro.report_text(home, data, found), end="")
    return 0


def cmd_calibrate(args):
    home = _home(args)
    data = limits.collect(home)
    recommendation = limits.recommend(data["completed"])
    if recommendation is None:
        print(f"not enough data: {len(data['completed'])} completed runs, "
              f"need at least {limits.MIN_SAMPLE}")
        return 1
    print(limits.report_text(home, data, recommendation), end="")
    if args.apply:
        saved = limits.save_limits(home, recommendation)
        print(f"wrote {limits.limits_path(home)}: "
              f"time_limit_s={saved['time_limit_s']} "
              f"op_limit={saved['op_limit']} "
              f"repair_cap={saved['repair_cap']}")
    else:
        print("dry run: re-run with --apply to write limits.json")
    return 0


def cmd_probe(args):
    home = _home(args)
    run = _run(home, args.task_id)
    _check_worktree(run)
    # The probes use the run's recorded adapter, not a fresh
    # detection: the run was admitted with one adapter, and
    # re-probing must measure that one.
    if not run.get("sandbox_adapter"):
        run["sandbox_adapter"] = sandbox.detect_adapter()
    probes = sandbox.run_probes(home, run, runner.run)
    run["probes"] = probes
    _save(home, run, "probed", {"probes": [
        {"name": p["name"], "passed": p["passed"]} for p in probes]})
    for probe in probes:
        print(f"{'PASS' if probe['passed'] else 'FAIL'} {probe['name']}: {probe['detail']}")
    print(f"sandbox adapter: {run['sandbox_adapter']}")
    return 0 if sandbox.probes_passed(probes) else 1


def cmd_stop(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] in ("local-verified", "stopped"):
        raise StateError(f"cannot stop from phase {run['phase']!r}")
    run["stop_reason"] = args.reason
    model.transition(run, "stopped")
    model.record_measurements(run)
    _save(home, run, "stopped", {"reason": args.reason})
    print(f"stopped: task={run['task_id']} — {args.reason}")
    return 0


def cmd_escalate(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] in ("local-verified", "stopped", "paused"):
        raise StateError(f"cannot escalate from phase {run['phase']!r}")
    run["prior_phase"] = run["phase"]
    run.setdefault("escalations", []).append({
        "question": args.question, "asked_at": time.time(),
        "answer": None, "answered_at": None,
    })
    model.transition(run, "paused")
    _save(home, run, "human-contact", {"question": args.question})
    print(f"escalated to human (run paused): {args.question}")
    return 0


PARSED_BY = ("raw", "schema", "extracted")


def cmd_act(args):
    """Factor 4: a worker's output is a tool call. Record it, validate it
    against the registry, then run the step it maps to. A malformed
    output is a failed step that costs an operation, never a guess."""
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] in ("local-verified", "stopped", "paused"):
        raise StateError(f"run is {run['phase']}; no worker output is accepted")
    model.check_budgets(run)
    kind = model.role_kind(run, args.role)
    with open(args.input, "rb") as fh:
        data = fh.read()
    seq = len(model.read_events(home, run["task_id"])) + 1
    ref = evidence.write_evidence(home, run["task_id"], f"worker-output-{seq}.json", data)
    detail = {"role": args.role, "output": ref, "parsed_by": args.parsed_by}
    if args.raw:
        with open(args.raw, "rb") as fh:
            detail["raw"] = evidence.write_evidence(
                home, run["task_id"], f"worker-raw-{seq}.txt", fh.read())
    try:
        tool, targs = tools.parse(data.decode("utf-8", "replace"), kind)
        model.check_session(run, args.role, args.session)
    except StateError as exc:
        model.bump_op(run)
        _save(home, run, "worker-output-rejected", {**detail, "error": str(exc)})
        raise
    if tool == "qa_report":
        model.bind_session(run, args.role, args.session)
    _save(home, run, "worker-output", {**detail, "tool": tool})
    step = dict(home=args.home, task_id=args.task_id, session=args.session, role=args.role)
    if tool == "red_test_ready":
        return cmd_red(argparse.Namespace(
            **step, test_path=targs["test_path"],
            command=shlex.join(targs["command"]), marker=targs["marker"],
            covers=targs["covers"]))
    if tool == "implementation_ready":
        return cmd_stage(argparse.Namespace(**step))
    if tool == "review":
        verdict = "approve" if targs["verdict"] == "merge-ready" else "changes-requested"
        return cmd_review(argparse.Namespace(**step, verdict=verdict, report=args.input))
    if tool == "escalate":
        return cmd_escalate(argparse.Namespace(
            home=args.home, task_id=args.task_id, question=targs["question"]))
    print(f"{tool}: recorded for {args.role}")
    return 0


def cmd_tool_schema(args):
    run = _run(_home(args), args.task_id)
    print(json.dumps(tools.json_schema(model.role_kind(run, args.role))))
    return 0


# ---------------------------------------------------------------- entry

def build_parser():
    parser = argparse.ArgumentParser(prog="t12", description="turbo-12f evidence runner")
    parser.add_argument("--version", action="version", version=f"t12 {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.add_argument("--repo", required=True); p.add_argument("--home", required=True)
    p.add_argument("--trunk", default="main", help="branch whose .t12/project.json every run follows")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prepare"); p.add_argument("--home", required=True); p.add_argument("--task", required=True)
    p.add_argument("--supersede", action="store_true",
                   help="archive a stopped run for this task and start a fresh one")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("context"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True); p.add_argument("--phase", required=True)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("red"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--test-path", required=True); p.add_argument("--command", required=True)
    p.add_argument("--marker", required=True); p.add_argument("--session", required=True,
                   help="test-writer session id (must differ from every other role's session)")
    p.add_argument("--covers", required=True,
                   help='JSON map of criterion index to the tests that check it, e.g. {"0": ["test_a"]}')
    p.set_defaults(func=cmd_red, role="test-writer")

    p = sub.add_parser("stage"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--session", required=True,
                   help="builder session id (must differ from the test-writer session)")
    p.set_defaults(func=cmd_stage, role="builder")

    p = sub.add_parser("gate"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--name", required=True); p.add_argument("--session", required=True,
                   help="QA session id (must differ from the builder session)")
    p.add_argument("--role", default="qa", help="a qa-kind role from the run (default qa)")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("replay"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("review"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True, help="a review role of the run")
    p.add_argument("--session", required=True,
                   help="reviewer session id (must differ from every other role's session)")
    p.add_argument("--verdict", required=True, choices=["approve", "changes-requested"])
    p.add_argument("--report", required=True, help="review report file (stored as evidence)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("green"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_green)

    p = sub.add_parser("manifest"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("verify"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("deliver"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--adapter", required=True, choices=list(delivery_mod.ADAPTERS))
    p.add_argument("--report", required=True, help="adapter report JSON (outcome, head, ref)")
    p.add_argument("--resolves", action="store_true",
                   help="this report resolves a previous uncertain outcome (read-back, not a new action)")
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("report"); p.add_argument("--home", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("board", help="one row per run: phase, budget left, and who acts next")
    p.add_argument("--home", required=True); p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("retro", help="recurring lessons across runs, each with a proposed check")
    p.add_argument("--home", required=True); p.add_argument("--json", action="store_true")
    p.add_argument("--min", type=int, default=retro.MIN_COUNT,
                   help=f"how often a lesson must recur to be proposed (default {retro.MIN_COUNT})")
    p.set_defaults(func=cmd_retro)

    p = sub.add_parser("calibrate"); p.add_argument("--home", required=True)
    p.add_argument("--apply", action="store_true",
                   help="write the recommended defaults to limits.json")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("status"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("probe"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("pause"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--answer", help="the human's answer to an open escalation (Factor 7)")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("stop"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("escalate"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--question", required=True)
    p.set_defaults(func=cmd_escalate)

    p = sub.add_parser("act"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True, help="a role of the run")
    p.add_argument("--session", required=True, help="the host's id for the worker session that produced the output")
    p.add_argument("--input", required=True, help="the worker's JSON output (stored as evidence)")
    p.add_argument("--raw", help="the model's whole reply, when --input was taken out of it "
                                 "(stored as evidence)")
    p.add_argument("--parsed-by", choices=PARSED_BY, default="raw",
                   help="how the host got --input from the reply: raw (it is the reply), "
                        "schema (the model CLI enforced the tool-call schema) or extracted "
                        "(the one tool call found in the reply's text)")
    p.set_defaults(func=cmd_act)

    p = sub.add_parser("tool-schema", help="JSON Schema of one tool call for a role of the run")
    p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True)
    p.set_defaults(func=cmd_tool_schema)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "home", None):
            home = os.path.realpath(args.home)
            if os.path.commonpath([os.path.realpath(PROJECT_ROOT), home]) == home:
                raise StateError(
                    f"refusing: this t12 ({PROJECT_ROOT}) runs from inside the state home; "
                    "a candidate never certifies itself, run the installed factory instead")
        if getattr(args, "task_id", None) is None or args.func in (cmd_status, cmd_context):
            return args.func(args)
        with model.run_lock(_home(args), args.task_id):
            return args.func(args)
    except T12Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
