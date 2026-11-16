"""turbo-12f CLI. Each command is one deterministic step: load the run
record from disk, validate, act, persist atomically. The LLM never
runs here; `context` prints the owned prompt + minimal context for
the operator to feed to a model, and the other commands verify what
the worker did in the worktree.
"""

import argparse
import copy
import fcntl
import json
import os
import sys
import time

from . import delivery as delivery_mod
from . import evidence, gates as gate_mod, gitio, globs, instructions, limits, model, retro, runner, sandbox, store, tools
from . import tdd as tdd_mod
from . import profile as profile_mod
from .errors import StateError, T12Error

VERSION = "0.2.0"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _lane_save(home, task_id, lane, event_type, detail=None, mutate=None):
    with model.run_lock(home, task_id):
        run = _run(home, task_id)
        if mutate is not None:
            mutate(run)
        model.sync_lane_row(run, lane)
        model.save_run(home, run)
        model.save_lane(home, lane, task_id)
        model.append_event(home, run, event_type, {**(detail or {}), "unit": lane["unit"]})


def _load_lane(home, run, unit):
    if unit is None:
        return None
    return model.load_lane(home, run["task_id"], unit)


def _lane_role(run, role, unit):
    kind = model.role_kind(run, role)
    if unit is not None and kind not in ("red", "green", "refactor"):
        raise StateError(f"the {kind} role does not work in a lane; "
                         "only red, green and refactor do")
    return kind


def _lane_run(home, run):
    run["budgets"]["ops_used"] += model.lane_ops_used(home, run["task_id"])
    return run


def _gate_cleared(g):
    return g["status"] in ("passed", "skipped") or (g["status"] == "failed" and g.get("advisory"))


def _gates_cleared(run):
    return all(_gate_cleared(g) for g in run["gates"])


def _next_gate(run):
    return next((g for g in run["gates"] if not _gate_cleared(g)), None)


def _check_gate_order(run, gate):
    for prior in run["gates"]:
        if prior is gate:
            break
        if not _gate_cleared(prior):
            raise StateError(
                f"gate {gate['name']!r} cannot run before gate {prior['name']!r} passes"
            )


def _check_worktree(run, lane=None):
    wt, branch = (run["worktree"], run["branch"]) if lane is None \
        else (lane["worktree"], lane["branch"])
    if not gitio.is_git_repo(wt):
        raise StateError(f"worktree missing or not a git repo: {wt}")
    if gitio.current_branch(wt) != branch:
        raise StateError(
            f"worktree is on {gitio.current_branch(wt)!r}, expected {branch!r}"
        )
    return wt


def _check_candidate_tree(run, action):
    wt = _check_worktree(run)
    if not gitio.is_clean(wt):
        raise StateError(f"refusing {action}: worktree has uncommitted changes")
    if gitio.head(wt) != run["candidate"]["commit"]:
        raise StateError(f"refusing {action}: HEAD moved since the final step")
    return wt


def _time_left(run):
    left = int(model.time_left_s(run))
    if left <= 0:
        raise StateError("time budget exhausted")
    return left


def _verification(home, run, worktree, unit=None):
    env = sandbox.verification_env(home, run["task_id"] if unit is None
                                   else f"{run['task_id']}@{unit}")
    ctx = {"worktree": worktree, "verify_home": env["HOME"],
           "adapter": run.get("sandbox_adapter"),
           "image": run.get("sandbox_image")}
    return env, ctx


def _run_on_commit(home, run, wt, commit, argv, unit=None):
    path = model.verify_checkout_dir(home, run["task_id"], unit)
    with gitio.detached_checkout(wt, path, commit) as checkout:
        env, ctx = _verification(home, run, checkout, unit)
        ctx["read_only"] = _link_deps(run, run["worktree"], checkout)
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


def _verify_evidence_unchanged(home, task_id, refs):
    for _, ref in refs:
        evidence.verify_ref(home, task_id, ref)


def _trunk_profile(repo, trunk_commit):
    return profile_mod.parse(gitio.file_at(repo, trunk_commit, profile_mod.PATH))


def _tdd_runner(home, run, lane=None):
    wt = run["worktree"] if lane is None else lane["worktree"]
    unit = None if lane is None else lane["unit"]

    def run_on_commit(run_, commit, argv, name):
        obs = _run_on_commit(home, run_, wt, commit, argv, unit)
        ref = evidence.write_observation(home, run_["task_id"], name, obs)
        return obs["exit_code"], obs["stdout"] + obs["stderr"], ref

    def save(event_type, detail=None):
        if lane is None:
            _save(home, run, event_type, detail)
        else:
            _lane_save(home, run["task_id"], lane, event_type, detail)

    return tdd_mod.Runner(run_on_commit, save)


LANE_SHARED = ("lanes", "roles", "bindings")


def _integration_runner(home, run):
    wt = run["worktree"]
    phase = [run["phase"]]

    def run_on_commit(run_, commit, argv, name):
        obs = _run_on_commit(home, run_, wt, commit, argv)
        ref = evidence.write_observation(home, run_["task_id"], name, obs)
        return obs["exit_code"], obs["stdout"] + obs["stderr"], ref

    def save(event_type, detail=None, lanes=(), mutate=None):
        task_id = run["task_id"]
        with model.run_lock(home, task_id):
            fresh = _run(home, task_id)
            if fresh["phase"] != phase[0]:
                raise StateError(f"run moved to {fresh['phase']!r} while integrating; "
                                 "nothing was recorded")
            if mutate is not None:
                mutate(fresh)
            fresh.update({k: v for k, v in run.items() if k not in LANE_SHARED})
            for lane in lanes:
                model.sync_lane_row(fresh, lane)
                model.save_lane(home, lane, task_id)
            model.save_run(home, fresh)
            model.append_event(home, fresh, event_type, detail)
            run.clear()
            run.update(fresh)
            phase[0] = run["phase"]

    return tdd_mod.Runner(run_on_commit, save)


def _lane_budget(home, run, lane):
    return tdd_mod.lane_budget_pause(run, lane, _tdd_runner(home, run, lane))


def _tdd_refs(tdd, prefix="tdd"):
    refs = []
    for entry in tdd.get("history") or []:
        where = f"{prefix} cycle {entry.get('cycle')}"
        for key in ("red", "green_tests", "green_lint", "refactor_tests", "refactor_lint"):
            if entry.get(key):
                refs.append((f"{where} {key}", entry[key]))
    if tdd.get("red"):
        refs.append((f"{prefix} red", tdd["red"]["output"]))
    final = tdd.get("final") or {}
    for key in ("tests", "lint"):
        if final.get(key):
            refs.append((f"{prefix} final {key}", final[key]))
    for merge in tdd.get("merges") or []:
        for key in ("tests", "lint"):
            if merge.get(key):
                refs.append((f"{prefix} merge {merge['unit']} {key}", merge[key]))
    findings = tdd.get("findings") or {}
    if findings.get("report"):
        refs.append((f"{prefix} findings ({findings.get('source')})", findings["report"]))
    return refs


def _evidence_refs(run):
    refs = _tdd_refs(run.get("tdd") or {})
    for gate in run["gates"]:
        if gate["observation"]:
            refs.append((f"gate:{gate['name']}", gate["observation"]))
    return refs


def _expected_head(run, phase):
    if phase == "ready":
        return run["base_commit"]
    if phase == "tdd":
        return run["tdd"]["last_good_commit"]
    if phase in ("gates", "review", "local-verified"):
        return run["candidate"]["commit"]
    return None


def _locked_tests_problem(tdd, wt):
    if not tdd.get("red_commit"):
        return None
    current = gitio.blob_map(wt, "HEAD", lambda p: globs.match(p, tdd["test_globs"]))
    changed = sorted(p for p in set(current) | set(tdd["locked"])
                     if current.get(p) != tdd["locked"].get(p))
    return ", ".join(changed) or None


def _unfinished_work(old):
    wt, base = old["worktree"], old["base_commit"]
    gitio.git_ok(wt, "add", "-A", "-N")
    test_globs = (old.get("tdd") or {}).get("test_globs") or []
    paths = [p for p in gitio.git_ok(wt, "diff", "--name-only", "--no-renames", "-z", base).split("\0")
             if p and not globs.match(p, test_globs)]
    if not paths:
        return ""
    return gitio.git_ok(wt, "--literal-pathspecs", "diff", base, "--", *paths, strip=False)


def _send_back(home, run, source, ref, detail):
    phase = model.repair_or_pause(run, source, ref)
    budgets = run["budgets"]
    detail = {**detail, "source": source, "report": ref["name"],
              "repairs_used": budgets["repairs_used"], "repair_cap": budgets["repair_cap"]}
    if phase == "tdd":
        _save(home, run, "tdd-repair", {**detail, "step": run["tdd"]["step"],
                                         "mode": run["tdd"]["mode"]})
    else:
        _save(home, run, "repair-cap", detail)
    return phase


def _last_compacted_error(home, run):
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
            if event.get("type") in ("gate-failed", "red-failed", "tdd-list-failed",
                                     "tdd-green-failed", "tdd-refactor-failed",
                                     "tdd-final-failed"):
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
    tdd = run.get("tdd")
    if tdd:
        summary = model.tdd_summary(run)
        lines.append(f"tdd: {summary['cycles']} cycles, "
                     f"{summary['items_done']}/{summary['items_total']} items done")
        lines.append(f"test command: {' '.join(tdd['test_command'])}")
        lines.append("lint command: " + (" ".join(tdd["lint_command"])
                                         if tdd.get("lint_command") else "not configured"))
        for item in tdd["items"]:
            built = f"  {item['test_file']}::{item['test_name']}" if item.get("test_file") else ""
            lines.append(f"  - {item['id']} [{item['status']}] {item['text']}{built}")
        for entry in tdd["history"]:
            if entry.get("red"):
                lines.append(f"red commit: {entry['commits']['red']}  item: {entry['item_id']}  "
                             f"output sha256={entry['red']['sha256'][:16]}")
        final = tdd.get("final")
        if final:
            lines.append(f"final run: tests exit={final['tests']['observation']['exit_code']} "
                         f"on {final['commit'][:12]} sha256={final['tests']['sha256'][:16]}")
            if final.get("lint"):
                lines.append(f"final lint: exit={final['lint']['observation']['exit_code']} "
                             f"sha256={final['lint']['sha256'][:16]}")
        lines.append("")
    if run["candidate"]:
        lines.append(f"candidate commit: {run['candidate']['commit']}")
        lines.append("")
    lines.append("Gates:")
    for gate in run["gates"]:
        obs = gate["observation"]
        extra = ""
        if obs:
            extra = f" exit={obs['observation']['exit_code']} sha256={obs['sha256'][:16]}"
        status = "advisory failed" if gate.get("advisory") and gate["status"] == "failed" else gate["status"]
        lines.append(f"  - {gate['name']}: {status}{extra}")
    lines.append("")
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
    if args.carry and not args.supersede:
        raise StateError("--carry only applies with --supersede")
    task = store.load_json(args.task)
    task_id = task.get("id")
    with model.run_lock(home, task_id):
        return _admit(home, config["repo"], config.get("trunk", "main"), task, task_id,
                      args.supersede, args.carry)


CARRIED_PATCH_CHARS = 12000


def _carry_from(home, old, archive):
    carried = {"from": archive, "patch": None,
               "answers": list((old.get("carried") or {}).get("answers") or []),
               "open_questions": [e["question"] for e in old.get("escalations") or []
                                  if e["answer"] is None]}
    carried["answers"] += [{"question": e["question"], "answer": e["answer"]}
                           for e in old.get("escalations") or [] if e["answer"] is not None]
    if os.path.isdir(old["worktree"]):
        patch = _unfinished_work(old)
        if patch.strip():
            carried["patch"] = evidence.write_evidence(
                home, old["task_id"], f"carried-{archive}.patch", patch.encode("utf-8"))
    return carried


def _admit(home, repo, trunk, task, task_id, supersede, carry=False):
    if not gitio.is_clean(repo):
        raise StateError(f"product repo has uncommitted changes: {repo}")
    base = task.get("base", "main")
    base_commit = gitio.rev_parse(repo, base)
    trunk_commit = gitio.rev_parse(repo, f"refs/heads/{trunk}")
    if not gitio.git_ok_bool(repo, "merge-base", trunk_commit, base_commit):
        raise StateError(f"base {base!r} shares no history with trunk {trunk!r}")
    if gitio.file_at(repo, trunk_commit, profile_mod.LEGACY_PATH) is not None:
        raise StateError(f"{profile_mod.LEGACY_PATH} is no longer read; move its "
                         f"security_review_paths into {profile_mod.PATH} on {trunk}")
    profile = _trunk_profile(repo, trunk_commit)
    task = profile_mod.apply(profile, task)
    for role in profile["roles"]:
        instructions.resolve(repo, trunk_commit, profile["roles"], role, PROJECT_ROOT)
    for gate in task.get("gates", []):
        model.check_gate_command(gate)
        try:
            sandbox.check_command(gate.get("command") or [])
        except T12Error as exc:
            raise StateError(
                f"gate {gate.get('name')!r} rejected at admission: {exc}"
            ) from exc
        extra = sorted(set(gate) - {"name", "command", "when", "advisory", "reruns", "base_check"})
        if extra:
            raise StateError(f"gate {gate.get('name')!r}: unknown keys {extra}; "
                             "a gate takes only name, command, when, advisory, reruns and base_check")
        if "advisory" in gate and not isinstance(gate["advisory"], bool):
            raise StateError(f"gate {gate.get('name')!r}: advisory must be a boolean")
        if "base_check" in gate and not isinstance(gate["base_check"], bool):
            raise StateError(f"gate {gate.get('name')!r}: base_check must be a boolean")
        reruns = gate.get("reruns", 0)
        if isinstance(reruns, bool) or not isinstance(reruns, int) or not 0 <= reruns <= 3:
            raise StateError(f"gate {gate.get('name')!r}: reruns must be an integer from 0 to 3")
        when = gate.get("when")
        if when is not None and not isinstance(when, dict):
            raise StateError(f"gate {gate.get('name')!r}: when must be an object")
        if when is not None and set(when) != {"files"}:
            raise StateError(f"gate {gate.get('name')!r}: when accepts only the key files")
        if when is not None and (not isinstance(when["files"], list) or not when["files"]
                                 or not all(isinstance(g, str) and g for g in when["files"])):
            raise StateError(f"gate {gate.get('name')!r}: when.files must be a non-empty list of globs")
        for g in (when or {}).get("files", []):
            try:
                globs.regex(g)
            except Exception as exc:
                raise StateError(f"gate {gate.get('name')!r}: when.files glob {g!r} "
                                 f"cannot compile: {exc}") from exc
    for key in ("test_command", "lint_command"):
        if task.get(key):
            try:
                sandbox.check_command(task[key])
            except T12Error as exc:
                raise StateError(f"{key} rejected at admission: {exc}") from exc
    supersedes = None
    carried = None
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
        if carry:
            carried = _carry_from(home, old, supersedes)
        gitio.remove_worktree_force(repo, old["worktree"])
        if gitio.branch_exists(repo, old["branch"]):
            gitio.delete_branch(repo, old["branch"])
        for row in old.get("lanes") or []:
            gitio.remove_worktree_force(repo, model.lane_worktree(home, task_id, row["unit"]))
            if gitio.branch_exists(repo, row["branch"]):
                gitio.delete_branch(repo, row["branch"])
    calibrated = limits.load_limits(home)
    adapter, image = sandbox.resolve_adapter(task)
    run = model.new_run(task, repo, home, base_commit, profile["roles"], supersedes=supersedes,
                        defaults=calibrated,
                        defaults_source="calibrated" if calibrated else "builtin",
                        sandbox_adapter=adapter, sandbox_image=image)
    for key in profile_mod.SCOPE_KEYS:
        run[key] = profile[key]
    run["review_criteria"] = profile.get("review_criteria", False)
    run["idle_after_s"] = profile.get("idle_after_s", profile_mod.DEFAULT_IDLE_AFTER_S)
    if profile.get("delivery"):
        run["delivery_adapter"] = profile["delivery"]["adapter"]
    run["trunk"] = trunk
    run["trunk_commit"] = trunk_commit
    run["tdd"] = model.new_tdd(profile, task, base_commit,
                               (calibrated or {}).get("max_lanes", model.DEFAULT_MAX_LANES))
    if carried:
        run["carried"] = carried
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


def _report_findings(home, run, ref):
    evidence.verify_ref(home, run["task_id"], ref)
    with open(ref["path"], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    try:
        findings = tools.parse(text, "review")[1].get("findings")
    except StateError:
        findings = None
    return findings if isinstance(findings, list) else text[-4000:]


def _ran(ref):
    obs = (ref or {}).get("observation")
    if not obs:
        return None
    return {"command": obs["argv"], "exit_code": obs["exit_code"], "sha256": ref["sha256"]}


def _analyzer_context(home, run):
    tdd = run["tdd"]
    findings = None
    if tdd["mode"] == "review-fix" and (tdd.get("findings") or {}).get("report"):
        findings = _report_findings(home, run, tdd["findings"]["report"])
    context = {
        "mode": tdd["mode"],
        "criteria": list(run["acceptance_criteria"]),
        "items": [{k: i[k] for k in ("id", "text", "kind", "status", "criteria")}
                  for i in tdd["items"]],
        "findings": findings,
        "files": gitio.tracked_files(run["worktree"])[:tdd_mod.FILE_LIST_LIMIT],
        "last_error": tdd.get("last_error"),
        "max_lanes": tdd.get("max_lanes", model.DEFAULT_MAX_LANES),
    }
    carried_patch = (run.get("carried") or {}).get("patch")
    if carried_patch:
        evidence.verify_ref(home, run["task_id"], carried_patch)
        with open(carried_patch["path"], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        context["carried_patch"] = {
            "from": run["carried"]["from"], "text": text[:CARRIED_PATCH_CHARS],
            "truncated": len(text) > CARRIED_PATCH_CHARS,
        }
    return context


def _review_context(home, run, role):
    prior_review = None
    for review in reversed(run.get("reviews") or []):
        if review["role"] == role:
            prior_review = {
                "verdict": review["verdict"],
                "commit": review["commit"],
                "report": review["report"]["name"],
                "voided": review["voided"],
                "voided_reason": review.get("voided_reason"),
                "findings": _report_findings(home, run, review["report"]),
            }
            break
    candidate = run["candidate"]["commit"] if run["candidate"] else None
    gates = []
    for g in run["gates"]:
        entry = {"name": g["name"], "status": g["status"]}
        ref = g.get("observation")
        if candidate and ref and g.get("commit") == candidate:
            entry.update(command=g["command"], commit=g["commit"],
                         exit_code=ref["observation"]["exit_code"], sha256=ref["sha256"])
        gates.append(entry)
    advisory_gates = []
    for g in run["gates"]:
        ref = g.get("observation")
        if g.get("advisory") and g["status"] == "failed" and ref:
            with open(ref["path"], encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-4000:]
            advisory_gates.append({"name": g["name"], "exit_code": ref["observation"]["exit_code"],
                                   "output_tail": tail})
    tdd = run["tdd"]
    final = tdd.get("final")
    summary = model.tdd_summary(run)
    wt = run["worktree"]
    return {
        "task_id": run["task_id"],
        "title": run["title"],
        "acceptance_criteria": run["acceptance_criteria"],
        "base": run["base"],
        "touches": run.get("touches"),
        "candidate": candidate,
        "advisory_gates": advisory_gates,
        "gates": gates,
        "tdd": {
            "cycles": summary["cycles"],
            "items": [{k: i[k] for k in ("id", "text", "kind", "status", "test_file",
                                          "test_name", "defeats")} for i in tdd["items"]],
            "final": None if not final or final["commit"] != candidate else {
                "commit": final["commit"], "tests": _ran(final["tests"]),
                "lint": _ran(final.get("lint"))},
            "lint_command": tdd.get("lint_command"),
        },
        "security_paths_touched": list(run.get("security_paths_touched") or []),
        "prior_review": prior_review,
        "changed_files": gitio.dirty_paths(wt) if os.path.isdir(wt) else [],
        "last_compacted_error": _last_compacted_error(home, run),
    }


def cmd_context(args):
    home = _home(args)
    run = _run(home, args.task_id)
    kind = _lane_role(run, args.role, args.unit)
    used = instructions.resolve(run["repo"], run["trunk_commit"], run["role_map"],
                                args.role, PROJECT_ROOT)
    lane = _load_lane(home, run, args.unit)
    tdd = lane or run.get("tdd")
    if tdd is None:
        raise StateError("this run has no tdd state; stop it and prepare it again with --supersede")
    if lane is not None:
        run = _lane_run(home, run)
    context = {
        "role": args.role,
        "tools": tools.schema_for(kind),
        "budgets": {
            "time_left_s": int(model.time_left_s(run)),
            "ops_left": run["budgets"]["op_limit"] - run["budgets"]["ops_used"],
            "repairs_left": run["budgets"]["repair_cap"] - run["budgets"]["repairs_used"],
        },
        "human_responses": (run.get("carried") or {}).get("answers", []) + [
            {"question": e["question"], "answer": e["answer"]}
            for e in (tdd if lane else run).get("escalations") or [] if e["answer"] is not None
        ],
        "worktree": tdd["worktree"] if lane else run["worktree"],
        "test_globs": list(tdd["test_globs"]),
        "protected_paths": run.get("protected_paths", []),
    }
    pins = _trunk_profile(run["repo"], run["trunk_commit"])
    context["skills"] = profile_mod.applicable_skills(pins, args.role, run["repo"],
                                                      run["base_commit"])
    if kind in ("green", "refactor"):
        context.update(tdd_mod.green_context(run, lane) if kind == "green"
                       else tdd_mod.refactor_context(run, lane))
    else:
        if kind == "analyzer":
            context.update(_analyzer_context(home, run))
        elif kind == "red":
            context.update(tdd_mod.red_context(run, lane))
        else:
            context.update(_review_context(home, run, args.role))
        context.update({
            "task_id": run["task_id"],
            "title": run["title"],
            "phase": run["phase"],
            "requested_phase": args.phase,
            "branch": tdd["branch"] if lane else run["branch"],
            "touches": run.get("touches"),
            "carried_open_questions": (run.get("carried") or {}).get("open_questions", []),
            "instructions": [{k: entry[k] for k in ("source", "path", "sha256")} for entry in used],
        })
        context.setdefault("criteria", list(run["acceptance_criteria"]))
    prompt, *skills = used
    print(prompt["text"])
    for skill in skills:
        print(f"--- skill: {skill['path']} ({skill['source']}) ---")
        print(skill["text"])
    print("--- context (JSON) ---")
    print(json.dumps(context, indent=2, sort_keys=True))
    return 0


def cmd_gate(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "gates":
        raise StateError(f"gate requires phase 'gates', run is {run['phase']!r}")
    model.check_budgets(run)
    _require_probes(run)
    wt = _check_candidate_tree(run, "gate")
    gate = gate_mod.find_gate(run, args.name) if args.name else _next_gate(run)
    if gate is None:
        raise StateError("every gate has passed on this candidate")
    _check_gate_order(run, gate)
    commit = run["candidate"]["commit"]
    when = gate.get("when")
    if when is not None and not any(globs.match(p, when["files"])
                                    for p in gitio.changed_paths(wt, run["base_commit"], commit)):
        gate["status"] = "skipped"
        gate["commit"] = commit
        if _gates_cleared(run):
            model.transition(run, "review")
        _save(home, run, "gate-skipped", {"gate": gate["name"]})
        print(f"gate {gate['name']}: skipped — no changed file matches when.files")
        return 0
    failed_runs = 0
    for _ in range(gate.get("reruns", 0) + 1):
        obs = _run_on_commit(home, run, wt, commit, gate["command"])
        failed = obs["timed_out"] or obs["exit_code"] != 0
        if not failed:
            break
        failed_runs += 1
    name = (f"gate-{gate['name']}-failed-{run['budgets']['repairs_used'] + 1}.txt" if failed
            else f"gate-{gate['name']}-output.txt")
    ref = evidence.write_observation(home, run["task_id"], name, obs)
    gate["commit"] = commit
    gate["observation"] = ref
    model.bump_op(run)
    if failed and gate.get("advisory"):
        gate["status"] = "failed"
        if _gates_cleared(run):
            model.transition(run, "review")
        _save(home, run, "gate-failed", {
            "step": gate["name"], "exit_code": obs["exit_code"],
            "duration_s": obs["duration_s"], "advisory": True,
            "output": (obs["stdout"] + obs["stderr"])[-4000:],
        })
        print(f"gate {gate['name']}: failed (advisory) — exit={obs['exit_code']}")
        return 0
    if failed:
        gate["status"] = "failed"
        _save(home, run, "gate-failed", {
            "step": gate["name"], "exit_code": obs["exit_code"],
            "duration_s": obs["duration_s"],
            "output": (obs["stdout"] + obs["stderr"])[-4000:],
        })
        if gate.get("base_check"):
            base_obs = _run_on_commit(home, run, wt, run["base_commit"], gate["command"])
            if base_obs["timed_out"] or base_obs["exit_code"] != 0:
                base_ref = evidence.write_observation(
                    home, run["task_id"], f"gate-{gate['name']}-base.txt", base_obs)
                question = (f"gate {gate['name']!r} fails on the candidate ({ref['name']}) and on "
                            f"the base commit ({base_ref['name']}): it was already red before "
                            "this change. Answer with how to proceed.")
                run["prior_phase"] = run["phase"]
                run.setdefault("escalations", []).append({
                    "question": question, "kind": "known-red", "gate": gate["name"],
                    "asked_at": time.time(), "answer": None, "answered_at": None,
                })
                model.transition(run, "paused")
                _save(home, run, "human-contact", {"question": question, "kind": "known-red"})
                print(f"gate {gate['name']}: known red — fails on the base commit too; "
                      "run paused for a human")
                return 0
        phase = _send_back(home, run, model.REPAIR_FROM_GATE_FAILURE, ref, {"gate": gate["name"]})
        b = run["budgets"]
        if phase == "paused":
            raise StateError(
                f"gate {gate['name']!r} failed (exit={obs['exit_code']}) and the repair cap "
                f"({b['repair_cap']}) is exceeded; run paused for a human — "
                f"the failing output is {ref['name']}")
        raise StateError(
            f"gate {gate['name']!r} failed (exit={obs['exit_code']}); back to tdd at "
            f"green, repairs used {b['repairs_used']}/{b['repair_cap']}")
    gate["status"] = "passed"
    if _gates_cleared(run):
        model.transition(run, "review")
    _save(home, run, "gate-passed", {"gate": gate["name"], "duration_s": obs["duration_s"]})
    if failed_runs:
        model.append_event(home, run, "gate-flaky", {"gate": gate["name"], "failed_runs": failed_runs})
    if run["phase"] == "review":
        print(f"gate {gate['name']}: passed — all gates passed, review next")
    else:
        print(f"gate {gate['name']}: passed — next gate: {_next_gate(run)['name']}")
    return 0


def _integrate(home, args):
    run = _run(home, args.task_id)
    lane = _load_lane(home, run, args.unit)
    model.check_budgets(_lane_run(home, copy.deepcopy(run)))
    _require_probes(run)
    _check_worktree(run, lane)
    paused = tdd_mod.lane_final(home, run, lane, runner=_tdd_runner(home, run, lane))
    if paused:
        print(paused)
        return 0
    with model.integration_lock(home, args.task_id):
        run = _run(home, args.task_id)
        _check_worktree(run)
        print(tdd_mod.integrate(home, run, lane, runner=_integration_runner(home, run)))
    return 0


def cmd_tdd_step(args):
    """The deterministic end of the tdd loop: every red test and lint on the
    tip commit, in the sandbox, then pin it as the candidate. With `--unit`,
    integrate that lane instead."""
    home = _home(args)
    if args.unit is not None:
        return _integrate(home, args)
    run = _run(home, args.task_id)
    model.check_budgets(run)
    _require_probes(run)
    _check_worktree(run)
    print(tdd_mod.final(home, run, runner=_tdd_runner(home, run)))
    return 0


def cmd_review(args):
    home = _home(args)
    run = _run(home, args.task_id)
    role = args.role
    kind = model.role_kind(run, role)
    if kind != "review":
        raise StateError(f"{role!r} is a {kind} role; only review roles record reviews")
    if run["phase"] != "review":
        raise StateError(f"review requires phase 'review', run is {run['phase']!r}")
    if not _gates_cleared(run):
        raise StateError("review requires all gates to pass first")
    model.check_budgets(run)
    _require_probes(run)
    model.check_session(run, role, args.session)
    wt = _check_worktree(run)
    commit = run["candidate"]["commit"]
    if gitio.head(wt) != commit:
        raise StateError("refusing review: HEAD moved since the final step")
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
        "commit": commit,
        "report": ref,
        "prior_review": prior["report"]["name"] if prior else None,
        "criteria": getattr(args, "criteria", None),
        "voided": False,
        "voided_reason": None,
        "at": time.time(),
    })
    _save(home, run, "reviewed", {"role": role, "verdict": args.verdict, "commit": commit[:12]})
    if args.verdict == "changes-requested":
        if _send_back(home, run, model.REPAIR_FROM_CHANGES_REQUESTED, ref, {"role": role}) == "paused":
            cap = run["budgets"]["repair_cap"]
            print(f"review: {role} requested changes on {commit[:12]} ({ref['name']}); the repair "
                  f"cap ({cap}) is exceeded, run paused for a human")
        else:
            print(f"review: {role} requested changes on {commit[:12]}; back to tdd at the analyzer")
        return 0
    if args.verdict == "human-gated":
        question = (f"{role} marked the candidate {commit[:12]} human-gated ({ref['name']}): "
                    "a human decision is needed before the review can continue. "
                    "Answer with the decision.")
        run["prior_phase"] = run["phase"]
        run.setdefault("escalations", []).append({
            "question": question, "kind": "human-gated", "asked_at": time.time(),
            "answer": None, "answered_at": None,
        })
        model.transition(run, "paused")
        _save(home, run, "human-contact", {"question": question, "kind": "human-gated"})
        print(f"review: {role} human-gated on {commit[:12]}; run paused for a human")
        return 0
    print(f"review: {role} {args.verdict} on {commit[:12]}")
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
        raise StateError("refusing deliver: HEAD moved since the final step")
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


def cmd_check(args):
    """Certification: the whole evidence chain re-verified, then local-verified."""
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] != "review":
        raise StateError(f"check requires phase 'review', run is {run['phase']!r}")
    model.check_budgets(run)
    _verify_chain_or_raise(home, run)
    wt = _check_worktree(run)
    if not gitio.is_clean(wt):
        raise StateError("refusing check: worktree has uncommitted changes")
    if gitio.head(wt) != run["candidate"]["commit"]:
        raise StateError("refusing check: HEAD moved since the final step")
    changed = _locked_tests_problem(run.get("tdd") or {}, wt)
    if changed:
        raise StateError(f"refusing check: test files changed since the red commit: {changed}")
    for gate in run["gates"]:
        advisory_failed = gate["status"] == "failed" and gate.get("advisory")
        if (gate["status"] == "skipped" or advisory_failed) \
                and gate.get("commit") != run["candidate"]["commit"]:
            raise StateError(f"refusing check: gate {gate['name']!r} was not "
                             f"{'skipped' if gate['status'] == 'skipped' else 'run'} on the candidate")
    evidence.verify_check(home, {**run, "gates": [
        g for g in run["gates"]
        if g["status"] != "skipped" and not (g["status"] == "failed" and g.get("advisory"))]})
    for gate in run["gates"]:
        if gate["status"] == "failed" and gate.get("advisory"):
            evidence.verify_ref(home, run["task_id"], gate.get("observation"))
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
    if run["phase"] not in ("review", "local-verified"):
        raise StateError(
            f"manifest requires phase 'review' or 'local-verified', run is {run['phase']!r}"
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

    refs = _evidence_refs(run)
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

    def observed(ref):
        return (ref or {}).get("observation") or {}

    def passing(ref):
        obs = observed(ref)
        return obs.get("exit_code") == 0 and not obs.get("timed_out")

    exit_problems = []
    tdd = run.get("tdd") or {}
    for entry in tdd.get("history") or []:
        where = f"tdd cycle {entry.get('cycle')}"
        if entry.get("red"):
            obs = observed(entry["red"])
            if obs.get("exit_code") == 0 or obs.get("timed_out"):
                exit_problems.append(f"{where}: the red run did not fail")
        for key in ("green_tests", "green_lint", "refactor_tests", "refactor_lint"):
            if entry.get(key) and not passing(entry[key]):
                exit_problems.append(f"{where}: {key} did not pass")
    for merge in tdd.get("merges") or []:
        for key in ("tests", "lint"):
            if merge.get(key) and not passing(merge[key]):
                exit_problems.append(f"merge of lane {merge['unit']}: {key} did not pass")
    final = tdd.get("final")
    if final:
        for key in ("tests", "lint"):
            if final.get(key) and not passing(final[key]):
                exit_problems.append(f"tdd final {key} did not pass")
        if run["candidate"] and final["commit"] != run["candidate"]["commit"]:
            exit_problems.append("tdd final run was not made on the candidate")
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
        changed = _locked_tests_problem(run.get("tdd") or {}, wt)
        if changed:
            wt_ok, wt_detail = False, f"test files changed since the red commit: {changed}"
        phase = run["phase"]
        if phase == "paused":
            phase = run.get("prior_phase")
        expected = _expected_head(run, phase)
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


def _lane_status(home, run, lane):
    return {
        "task_id": run["task_id"],
        "unit": lane["unit"],
        "status": lane["status"],
        "branch": lane["branch"],
        "worktree": lane["worktree"],
        "owns": lane["owns"],
        "tests": lane["tests"],
        "after": lane["after"],
        "fork_commit": lane["fork_commit"],
        "tdd": {
            "step": lane["step"],
            "cycle": lane["cycle"],
            "items_done": sum(1 for i in lane["items"] if i["status"] == "done"),
            "items_total": len(lane["items"]),
            "attempts": lane["attempts"],
            "red_commit": lane["red_commit"],
            "last_good_commit": lane["last_good_commit"],
        },
        "ops_used": lane["ops_used"],
        "budget": lane["budget"],
        "next": model.lane_next(run, lane, model.list_lanes(home, run["task_id"])),
    }


def cmd_status(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if args.unit is not None:
        print(json.dumps(_lane_status(home, run, _load_lane(home, run, args.unit)),
                         indent=2, sort_keys=True))
        return 0
    tdd = run.get("tdd")
    print(json.dumps({
        "task_id": run["task_id"],
        "title": run["title"],
        "phase": run["phase"],
        "branch": run["branch"],
        "worktree": run["worktree"],
        "tdd": None if not tdd else {
            "step": tdd["step"],
            "mode": tdd["mode"],
            "cycle": tdd["cycle"],
            "items_done": sum(1 for i in tdd["items"] if i["status"] == "done"),
            "items_total": len(tdd["items"]),
            "attempts": tdd["attempts"],
            "red_commit": tdd["red_commit"],
            "last_good_commit": tdd["last_good_commit"],
            "test_globs": tdd["test_globs"],
            "pool": tdd.get("pool"),
            "reserve": tdd.get("reserve"),
            "pending_merge": tdd.get("pending_merge"),
            "integrations": tdd.get("integrations", 0),
            "integration_failures": tdd.get("integration_failures") or [],
        },
        "lanes": run.get("lanes") or [],
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
            "cycle_limit": run["budgets"]["cycle_limit"],
            "attempt_limit": run["budgets"]["attempt_limit"],
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


def _resume_lane(args):
    home = _home(args)
    task_id = args.task_id
    run = _run(home, task_id)
    lane = _load_lane(home, run, args.unit)
    if run["phase"] != "tdd":
        raise StateError(f"resume --unit needs the run in phase 'tdd', it is {run['phase']!r}; "
                         "resume the run first")
    if lane["status"] != "paused":
        raise StateError(f"lane {args.unit} is {lane['status']!r}, not paused")
    if args.repair_cap is not None:
        raise StateError("--repair-cap belongs to the run: resume without --unit")
    pending = model.open_escalation(lane)
    answer = (args.answer or "").strip()
    if pending and not answer:
        raise StateError(f"lane {args.unit} is waiting on a human answer to: "
                         f"{pending['question']!r}; resume with --answer")
    if answer and not pending:
        raise StateError("no open question to answer; resume without --answer")
    limit = tdd_mod.cycle_limit(run, lane)
    cycle_limit = limit if args.cycle_limit is None else args.cycle_limit
    if cycle_limit < limit:
        raise StateError(f"--cycle-limit can only raise the limit (now {limit})")
    kind = (pending or {}).get("kind")
    dimension = (pending or {}).get("dimension")
    if (kind == "cycle-limit" or (kind == tdd_mod.LANE_BUDGET and dimension == "cycles")) \
            and lane["cycle"] >= cycle_limit:
        raise StateError(
            f"refusing resume: {lane['cycle']} cycles are done and the cycle limit is "
            f"{cycle_limit}; raise it: resume --cycle-limit N with N > {lane['cycle']}")
    ops = (lane.get("budget") or {}).get("op_limit")
    op_limit = ops if args.op_limit is None else args.op_limit
    if args.op_limit is not None and (ops is None or op_limit <= ops):
        raise StateError(f"--op-limit can only raise the lane's operation share (now {ops})")
    if kind == tdd_mod.LANE_BUDGET and dimension == "ops" and lane["ops_used"] >= op_limit:
        raise StateError(
            f"refusing resume: {lane['ops_used']} operations are spent and the lane's share is "
            f"{op_limit}; raise it: resume --op-limit N with N > {lane['ops_used']}")
    wt = _check_worktree(run, lane)
    if gitio.head(wt) != lane["last_good_commit"]:
        raise StateError(
            f"refusing resume: HEAD moved while paused (expected "
            f"{lane['last_good_commit'][:12]}, found {gitio.head(wt)[:12]})")
    changed = _locked_tests_problem(lane, wt)
    if changed:
        raise StateError(f"refusing resume: test files changed while paused: {changed}")
    _verify_evidence_unchanged(home, task_id, _tdd_refs(lane, f"lane {args.unit}"))
    with model.run_lock(home, task_id):
        _verify_chain_or_raise(home, _run(home, task_id))
    if cycle_limit != limit:
        lane["budget"] = {**lane["budget"], "cycle_limit": cycle_limit}
    if op_limit != ops:
        lane["budget"] = {**lane["budget"], "op_limit": op_limit}
    if pending:
        pending["answer"] = answer
        pending["answered_at"] = time.time()
        _lane_save(home, task_id, lane, "human-response",
                   {"question": pending["question"], "answer": answer})
        if pending.get("kind") == "attempt-limit":
            lane["attempts"] = {step: 0 for step in lane["attempts"]}
    lane["status"] = "done" if lane["step"] == "final" else "active"
    _lane_save(home, task_id, lane, "resumed", {"phase": run["phase"], "cycle_limit": cycle_limit})
    print(f"resumed: task={task_id} lane={args.unit}")
    return 0


def cmd_resume(args):
    if args.unit is not None:
        return _resume_lane(args)
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
    budgets = run["budgets"]
    repair_cap = budgets["repair_cap"] if args.repair_cap is None else args.repair_cap
    cycle_limit = budgets["cycle_limit"] if args.cycle_limit is None else args.cycle_limit
    if repair_cap < budgets["repair_cap"] or cycle_limit < budgets["cycle_limit"]:
        raise StateError("--repair-cap and --cycle-limit can only raise a limit "
                         f"(now {budgets['repair_cap']} and {budgets['cycle_limit']})")
    if budgets["repairs_used"] > repair_cap:
        raise StateError(
            f"refusing resume: {budgets['repairs_used']} repairs are used and the repair cap is "
            f"{repair_cap}, so every step would be refused again; the operator decides whether "
            f"the run may try more: resume --repair-cap N with N >= {budgets['repairs_used']}")
    if pending and pending.get("kind") == "cycle-limit" and run["tdd"]["cycle"] >= cycle_limit:
        raise StateError(
            f"refusing resume: {run['tdd']['cycle']} cycles are done and the cycle limit is "
            f"{cycle_limit}; raise it: resume --cycle-limit N with N > {run['tdd']['cycle']}")
    prior = run["prior_phase"]
    wt = _check_worktree(run)
    expected = _expected_head(run, prior)
    if expected is not None and gitio.head(wt) != expected:
        raise StateError(
            f"refusing resume: HEAD moved while paused "
            f"(expected {expected[:12]}, found {gitio.head(wt)[:12]})"
        )
    changed = _locked_tests_problem(run.get("tdd") or {}, wt)
    if changed:
        raise StateError(f"refusing resume: test files changed while paused: {changed}")
    _verify_evidence_unchanged(home, run["task_id"], _evidence_refs(run))
    _verify_chain_or_raise(home, run)
    budgets["repair_cap"] = repair_cap
    budgets["cycle_limit"] = cycle_limit
    if pending:
        pending["answer"] = answer
        pending["answered_at"] = time.time()
        _save(home, run, "human-response", {"question": pending["question"], "answer": answer})
        if pending.get("kind") == "attempt-limit":
            run["tdd"]["attempts"] = {step: 0 for step in run["tdd"]["attempts"]}
        if pending.get("kind") == "known-red":
            next(g for g in run["gates"] if g["name"] == pending["gate"])["status"] = "pending"
    model.transition(run, prior)
    run["prior_phase"] = None
    _save(home, run, "resumed", {"phase": prior, "repair_cap": repair_cap,
                                 "cycle_limit": cycle_limit})
    print(f"resumed: task={run['task_id']} phase={prior}")
    return 0


def cmd_report(args):
    home = _home(args)
    data = limits.collect(home)
    recommendation = limits.recommend(data["completed"])
    if args.json:
        print(json.dumps(
            {"home": home, "report": data, "recommendation": recommendation,
             "lanes": limits.recommend_lanes(data)},
            indent=2, sort_keys=True, default=str))
    else:
        print(limits.report_text(home, data, recommendation), end="")
    return 0


def _lane_rows(home, run, done):
    b = run["budgets"]
    ops_left = b["op_limit"] - b["ops_used"] - model.lane_ops_used(home, run["task_id"])
    pending = model.pending_merge(run)
    rows = []
    lanes = model.list_lanes(home, run["task_id"])
    for lane in lanes:
        if lane["status"] == "merged" or (pending and pending["unit"] == lane["unit"]):
            continue
        nxt = model.lane_next(run, lane, lanes)
        rows.append({
            "task_id": run["task_id"],
            "unit": lane["unit"],
            "phase": run["phase"],
            "candidate": None,
            "tdd_step": lane["step"],
            "cycle": lane["cycle"],
            "gates_passed": 0,
            "time_left_s": None if done else int(model.time_left_s(run)),
            "ops_left": None if done else ops_left,
            "repairs_left": None if done else b["repair_cap"] - b["repairs_used"],
            "busy": model.is_lane_busy(home, run["task_id"], lane["unit"]),
            "next": nxt,
            "waiting": nxt.removeprefix("waiting: ") if nxt.startswith("waiting: ") else None,
            "max_lanes": None,
        })
    return rows


def _runner_alive(home):
    try:
        with open(os.path.join(home, "runner.lock")) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        pass
    return False


def _idle_s(home, task_id):
    try:
        events = model.read_events(home, task_id)
    except (OSError, StateError):
        return None
    if not events:
        return None
    return int(time.time() - events[-1]["ts"])


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
        busy = model.is_busy(home, run["task_id"])
        idle_s = None if run["phase"] != "gates" or busy or _runner_alive(home) else _idle_s(home, run["task_id"])
        idle_after_s = run.get("idle_after_s", profile_mod.DEFAULT_IDLE_AFTER_S)
        rows.append({
            "task_id": run["task_id"],
            "phase": run["phase"],
            "candidate": (run["candidate"] or {}).get("commit"),
            "tdd_step": (run.get("tdd") or {}).get("step"),
            "cycle": (run.get("tdd") or {}).get("cycle"),
            "gates_passed": sum(g["status"] in ("passed", "skipped") for g in run["gates"]),
            "time_left_s": None if done else int(model.time_left_s(run)),
            "ops_left": None if done else b["op_limit"] - b["ops_used"] - model.lane_ops_used(
                home, run["task_id"]),
            "repairs_left": None if done else b["repair_cap"] - b["repairs_used"],
            "busy": busy,
            "next": model.next_step(run),
            "unit": None,
            "idle_s": idle_s if idle_s is not None and idle_s > idle_after_s else None,
            "waiting": None,
            "idle": (idle_s or 0) > idle_after_s,
            "max_lanes": (run.get("tdd") or {}).get("max_lanes"),
        })
        if run["phase"] == "tdd":
            rows += _lane_rows(home, run, done)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    header = ("TASK", "PHASE", "CANDIDATE", "TIME", "OPS", "REPAIRS", "NEXT")
    table = [header] + [(
        r["task_id"] + (f"@{r['unit']}" if r["unit"] else ""),
        r["phase"] + (" (busy)" if r["busy"] else "") + (" idle" if r.get("idle") else ""),
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
        saved = limits.save_limits(home, recommendation, limits.recommend_lanes(data))
        print(f"wrote {limits.limits_path(home)}: "
              f"time_limit_s={saved['time_limit_s']} "
              f"op_limit={saved['op_limit']} "
              f"repair_cap={saved['repair_cap']}"
              + (f" max_lanes={saved['max_lanes']}" if "max_lanes" in saved else ""))
    else:
        print("dry run: re-run with --apply to write limits.json")
    return 0


def cmd_probe(args):
    home = _home(args)
    run = _run(home, args.task_id)
    _check_worktree(run)
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


def _pause_run(run, question):
    run["prior_phase"] = run["phase"]
    run.setdefault("escalations", []).append({
        "question": question, "asked_at": time.time(),
        "answer": None, "answered_at": None,
    })
    model.transition(run, "paused")


def cmd_escalate(args):
    home = _home(args)
    run = _run(home, args.task_id)
    if run["phase"] in ("local-verified", "stopped", "paused"):
        raise StateError(f"cannot escalate from phase {run['phase']!r}")
    if getattr(args, "unit", None) is not None:
        lane = _load_lane(home, run, args.unit)
        lane["escalations"].append({
            "question": args.question, "kind": "escalation", "asked_at": time.time(),
            "answer": None, "answered_at": None})
        lane["status"] = "paused"
        _lane_save(home, args.task_id, lane, "human-contact", {"question": args.question})
        print(f"escalated to human (lane {args.unit} paused): {args.question}")
        return 0
    _pause_run(run, args.question)
    _save(home, run, "human-contact", {"question": args.question})
    print(f"escalated to human (run paused): {args.question}")
    return 0


PARSED_BY = ("raw", "schema", "extracted", "host-tool")


TDD_HANDLERS = {
    "test_list_ready": tdd_mod.analyze,
    "red_test_ready": tdd_mod.red,
    "no_cheat_left": tdd_mod.no_cheat_left,
    "green_ready": tdd_mod.green,
    "refactor_done": tdd_mod.refactor,
}
PATCH_TOOLS = ("green_ready", "refactor_done")
REVIEW_VERDICTS = {"merge-ready": "approve", "blocked": "changes-requested",
                   "unverified": "changes-requested", "human-gated": "human-gated"}


def _criteria_entry_problems(run, entry):
    """What is wrong with one criteria entry of a review call."""
    if not isinstance(entry, dict):
        return ["entry must be an object"]
    problems = []
    if "index" not in entry:
        problems.append("index is missing")
    else:
        index = entry["index"]
        if (not isinstance(index, int) or isinstance(index, bool)
                or not 0 <= index < len(run["acceptance_criteria"])):
            problems.append(f"index {index} is not a criterion of this run")
    if "result" not in entry:
        problems.append("result is missing")
    elif entry["result"] not in ("pass", "fail"):
        problems.append(f"result must be \"pass\" or \"fail\", got \"{entry['result']}\"")
    for key in entry:
        if key not in ("index", "result"):
            problems.append(f"unexpected key \"{key}\"")
    return problems


def _refusal(verdict, detail):
    return StateError(f"{verdict} is refused: {detail}")


def _check_review_criteria(run, targs):
    """Refuse a review whose criteria results contradict its verdict or
    leave out a criterion of this run."""
    criteria = targs.get("criteria")
    if criteria is None:
        if run.get("review_criteria"):
            raise _refusal(targs["verdict"], "criteria are required")
        return
    verdict = targs["verdict"]
    for position, entry in enumerate(criteria):
        problems = _criteria_entry_problems(run, entry)
        if problems:
            raise _refusal(verdict, f"criteria[{position}]: {'; '.join(problems)}")
    if verdict == "merge-ready":
        for position, entry in enumerate(criteria):
            if entry["result"] == "fail":
                raise _refusal(verdict, f"criteria[{position}]: "
                                        f"criterion {entry['index']} result is \"fail\"")
    listed = [entry["index"] for entry in criteria]
    for position, index in enumerate(listed):
        if index in listed[:position]:
            raise _refusal(verdict, f"criteria[{position}]: index {index} is repeated")
    for index in range(len(run["acceptance_criteria"])):
        if index not in listed:
            raise _refusal(verdict, f"index {index} is missing from criteria")


def _worker_patch(tool, path):
    if tool not in PATCH_TOOLS:
        if path is not None:
            raise StateError(f"--patch applies only to {' and '.join(PATCH_TOOLS)}, not {tool}")
        return None
    if path is None:
        raise StateError(f"{tool} needs --patch FILE: the diff of the blind worktree "
                         "(an empty file when nothing changed)")
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise StateError(f"cannot read --patch {path}: {exc}") from exc


def cmd_act(args):
    """Factor 4: a worker's output is a tool call. Record it, validate it
    against the registry, then run the step it maps to. A malformed
    output is a failed step that costs an operation, never a guess. With
    `--unit` the call is one lane's: its record, worktree and events."""
    home = _home(args)
    run = _run(home, args.task_id)
    if getattr(args, "run_id", None) and args.run_id != run.get("run_id"):
        raise model.reject_superseded(home, args.task_id, args.run_id)
    lane = _load_lane(home, run, args.unit)
    if run["phase"] in ("local-verified", "stopped", "paused"):
        raise StateError(f"run is {run['phase']}; no worker output is accepted")
    integrating = _integration_green_pending(run) and lane is None
    if integrating != getattr(args, "integration_locked", False):
        raise StateError("the run moved between locking and acting; retry the call")
    if lane is not None:
        if lane["status"] != "active":
            raise StateError(f"lane {args.unit} is {lane['status']}; no worker output is accepted")
        run = _lane_run(home, run)
    model.check_budgets(run)
    kind = _lane_role(run, args.role, args.unit)
    with open(args.input, "rb") as fh:
        data = fh.read()
    if lane is None:
        seq, prefix = len(model.read_events(home, run["task_id"])) + 1, ""
    else:
        lane["acts"] += 1
        seq, prefix = lane["acts"], f"{args.unit}/"
    integration = _integration_runner(home, run) if integrating else None

    def record(event_type, detail, bind=None):
        if integration is not None:
            integration.save(event_type, detail, mutate=bind)
        else:
            if bind is not None:
                bind(run)
            _save(home, run, event_type, detail)

    ref = evidence.write_evidence(home, run["task_id"], f"{prefix}worker-output-{seq}.json", data)
    detail = {"role": args.role, "output": ref, "parsed_by": args.parsed_by}
    if args.raw:
        with open(args.raw, "rb") as fh:
            detail["raw"] = evidence.write_evidence(
                home, run["task_id"], f"{prefix}worker-raw-{seq}.txt", fh.read())
    try:
        tool, targs = tools.parse(data.decode("utf-8", "replace"), kind)
        model.check_session(run, args.role, args.session)
        if tool == "review":
            _check_review_criteria(run, targs)
        patch = _worker_patch(tool, args.patch)
        if patch is not None:
            detail["patch"] = evidence.write_evidence(
                home, run["task_id"], f"{prefix}worker-patch-{seq}.patch", patch)
            try:
                patch = patch.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StateError(f"the patch is not valid UTF-8: {exc}") from exc
        if integrating and tool not in ("green_ready", "escalate"):
            raise StateError("the run is at integration green: only the green role's green_ready "
                             f"or escalate is accepted, not {tool}")
    except StateError as exc:
        if lane is None:
            model.bump_op(run)
            record("worker-output-rejected", {**detail, "error": str(exc)})
        else:
            lane["ops_used"] += 1
            _lane_save(home, run["task_id"], lane, "worker-output-rejected",
                       {**detail, "error": str(exc)})
            _lane_budget(home, run, lane)
        raise
    if lane is None:
        record("worker-output", {**detail, "tool": tool},
               bind=lambda target: model.bind_session(target, args.role, args.session))
    else:
        try:
            _lane_save(home, run["task_id"], lane, "worker-output", {**detail, "tool": tool},
                       mutate=lambda fresh: model.bind_session(fresh, args.role, args.session))
        except StateError as exc:
            lane["ops_used"] += 1
            _lane_save(home, run["task_id"], lane, "worker-output-rejected",
                       {**detail, "error": str(exc)})
            raise
    step = dict(home=args.home, task_id=args.task_id, session=args.session, role=args.role)
    if tool == "review":
        return cmd_review(argparse.Namespace(
            **step, verdict=REVIEW_VERDICTS[targs["verdict"]],
            report=args.input, criteria=targs.get("criteria")))
    if tool == "escalate":
        if integrating:
            _pause_run(run, targs["question"])
            integration.save("human-contact", {"question": targs["question"]})
            print(f"escalated to human (run paused): {targs['question']}")
            return 0
        return cmd_escalate(argparse.Namespace(
            home=args.home, task_id=args.task_id, question=targs["question"], unit=args.unit))
    _require_probes(run)
    _check_worktree(run, lane)
    if tool in ("test_list_ready", "no_cheat_left"):
        if lane is None:
            model.bump_op(run)
        else:
            lane["ops_used"] += 1
    extra = {"patch": patch} if tool in PATCH_TOOLS else {}
    runner_ = integration or _tdd_runner(home, run, lane)
    try:
        message = TDD_HANDLERS[tool](home, run, args.role, args.session, targs,
                                     runner=runner_, lane=lane, **extra)
    except StateError as exc:
        if lane is not None:
            if tool == "green_ready" and tdd_mod.scope_refused_after_integration(lane):
                with model.integration_lock(home, args.task_id):
                    fresh = _run(home, args.task_id)
                    moved = tdd_mod.to_integration_green(
                        home, fresh, lane, runner=_integration_runner(home, fresh))
                raise StateError(f"{exc}; {moved}") from exc
            _lane_budget(home, run, lane)
        raise
    if lane is not None:
        message = _lane_budget(home, run, lane) or message
    print(message)
    return 0


def cmd_tool_schema(args):
    home = _home(args)
    run = _run(home, args.task_id)
    kind = _lane_role(run, args.role, args.unit)
    _load_lane(home, run, args.unit)
    print(json.dumps(tools.host_tools(kind) if args.host_tools else tools.json_schema(kind)))
    return 0


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
    p.add_argument("--carry", action="store_true",
                   help="with --supersede: keep the old run's answered questions, and give the "
                        "analyzer its unfinished work as a patch to start from")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("context"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True); p.add_argument("--phase", required=True)
    p.add_argument("--unit", help="a lane of the run: its worktree, items and write scope")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("tdd-step", help="the deterministic end of the tdd loop: every red test "
                                        "and lint on the tip commit, then pin the candidate")
    p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--unit", help="a lane of the run (integrate)")
    p.set_defaults(func=cmd_tdd_step)

    p = sub.add_parser("gate"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--name", help="a gate of the run (default: the next pending one)")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("review"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True, help="a review role of the run")
    p.add_argument("--session", required=True,
                   help="reviewer session id (must differ from every other role's session)")
    p.add_argument("--verdict", required=True, choices=["approve", "changes-requested", "human-gated"])
    p.add_argument("--report", required=True, help="review report file (stored as evidence)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("check", help="certify: re-verify the whole evidence chain, then local-verified")
    p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_check)

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
    p.add_argument("--unit", help="one lane of the run")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("probe"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("pause"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume"); p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--answer", help="the human's answer to an open escalation (Factor 7)")
    p.add_argument("--repair-cap", type=int,
                   help="raise the repair cap; required to resume after a repair-cap pause")
    p.add_argument("--cycle-limit", type=int,
                   help="raise the cycle limit; required to resume after a cycle-limit pause")
    p.add_argument("--unit", help="resume one paused lane instead of the run")
    p.add_argument("--op-limit", type=int,
                   help="with --unit: raise the lane's operation share; required to resume a "
                        "lane that spent it")
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
    p.add_argument("--run-id", help="the run the worker started on; output for any other run "
                                    "is rejected as superseded")
    p.add_argument("--raw", help="the model's whole reply, when --input was taken out of it "
                                 "(stored as evidence)")
    p.add_argument("--parsed-by", choices=PARSED_BY, default="raw",
                   help="how the host got --input from the reply: raw (it is the reply), "
                        "schema (the model CLI enforced the tool-call schema), extracted "
                        "(the one tool call found in the reply's text) or host-tool (the "
                        "model called the factory's tool, registered with its schema)")
    p.add_argument("--patch", help="the blind worktree's diff, for green_ready and refactor_done "
                                   "(required for those, refused for every other call)")
    p.add_argument("--unit", help="a lane of the run: the call is that lane's")
    p.set_defaults(func=cmd_act)

    p = sub.add_parser("tool-schema", help="JSON Schema of one tool call for a role of the run")
    p.add_argument("--home", required=True); p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--unit", help="a lane of the run")
    p.add_argument("--host-tools", action="store_true",
                   help="print one definition per tool instead, for agent hosts that "
                        "register the factory's tools as real tools")
    p.set_defaults(func=cmd_tool_schema)

    return parser


LOCKLESS_COMMANDS = (cmd_status, cmd_context, cmd_tool_schema)
LANE_LOCKED_COMMANDS = (cmd_act, cmd_tdd_step, cmd_resume)


def _integration_green_pending(run):
    return run["phase"] == "tdd" and (run.get("tdd") or {}).get("step") == model.INTEGRATION_GREEN


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "home", None):
            home = os.path.realpath(args.home)
            if os.path.commonpath([os.path.realpath(PROJECT_ROOT), home]) == home:
                raise StateError(
                    f"refusing: this t12 ({PROJECT_ROOT}) runs from inside the state home; "
                    "a candidate never certifies itself, run the installed factory instead")
        if getattr(args, "task_id", None) is None or args.func in LOCKLESS_COMMANDS:
            return args.func(args)
        unit = getattr(args, "unit", None)
        if unit is not None and args.func in LANE_LOCKED_COMMANDS:
            with model.lane_lock(_home(args), args.task_id, unit):
                return args.func(args)
        if unit is None and args.func is cmd_act and _integration_green_pending(
                _run(_home(args), args.task_id)):
            with model.integration_lock(_home(args), args.task_id):
                args.integration_locked = True
                return cmd_act(args)
        with model.run_lock(_home(args), args.task_id):
            return args.func(args)
    except T12Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
