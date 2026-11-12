"""Worker outputs as tool calls (Factor 4). A worker's whole output is
one JSON object, {"tool": name, "args": {...}}, checked here against
the registry before anything acts on it. The registry mirrors the
output contracts in prompts/*.md. Pure: no I/O, no run state.
"""

import json

from .errors import StateError

STR = "string"
STR_LIST = "list of strings"
FINDINGS = "list of findings"
FINDING_FIELDS = ("id", "class", "severity", "where", "finding", "owner")
ARGV = "non-empty list of strings"
COVERS = "map of criterion index to test names"

ESCALATE = {"escalate": {"question": STR}}

# Keyed by kind, the part of a role the core knows how to enforce. A
# project names its own roles, but each maps to one of these kinds.
TOOLS = {
    "test-writer": {
        "red_test_ready": {"test_path": STR, "command": ARGV, "marker": STR, "covers": COVERS},
        **ESCALATE,
    },
    "builder": {
        "implementation_ready": {"summary": STR},
        **ESCALATE,
    },
    "qa": {
        "qa_report": {"verdict": ("pass", "fail", "unverified"),
                      "commands": STR_LIST, "failures": STR_LIST},
        **ESCALATE,
    },
    "review": {
        "review": {"verdict": ("merge-ready", "blocked", "human-gated", "unverified"),
                   "findings": FINDINGS, "verification": STR},
        **ESCALATE,
    },
}
KINDS = tuple(TOOLS)
ALL_TOOLS = sorted({name for tools in TOOLS.values() for name in tools})


def _check(name, value, kind):
    if isinstance(kind, tuple):
        return None if value in kind else f"{name} must be one of {list(kind)}"
    if kind == STR:
        ok = isinstance(value, str) and value.strip() != ""
        return None if ok else f"{name} must be a non-empty string"
    if kind in (STR_LIST, ARGV):
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        ok = ok and (kind == STR_LIST or value != [])
        return None if ok else f"{name} must be a {kind}"
    if kind == COVERS:
        ok = isinstance(value, dict) and all(
            isinstance(v, list) and v and all(isinstance(t, str) for t in v)
            for v in value.values())
        return None if ok else f"{name} must be a {kind}"
    ok = isinstance(value, list) and all(isinstance(v, dict) for v in value)
    return None if ok else f"{name} must be a list of objects"


def parse(text, kind):
    """Return (tool, args) or raise StateError listing every problem."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(f"worker output is not valid JSON: {exc}") from exc
    if (not isinstance(data, dict) or set(data) != {"tool", "args"}
            or not isinstance(data["tool"], str) or not isinstance(data["args"], dict)):
        raise StateError('worker output must be exactly {"tool": ..., "args": {...}}')
    tool = data["tool"]
    if tool not in ALL_TOOLS:
        raise StateError(f"unknown tool {tool!r}; tools are {ALL_TOOLS}")
    spec = TOOLS[kind].get(tool)
    if spec is None:
        raise StateError(f"tool {tool!r} is not allowed for a {kind} role; "
                         f"it may call {sorted(TOOLS[kind])}")
    args = data["args"]
    problems = [f"missing arg {k!r}" for k in spec if k not in args]
    problems += [f"unexpected arg {k!r}" for k in args if k not in spec]
    problems += [p for k, shape in spec.items() if k in args
                 for p in [_check(k, args[k], shape)] if p]
    if problems:
        raise StateError(f"invalid {tool!r} call: " + "; ".join(problems))
    return tool, args


def schema_for(kind):
    """The tools a role of this kind may call, for its context window."""
    return [
        {"name": name, "args": {k: list(v) if isinstance(v, tuple) else v
                                for k, v in spec.items()}}
        for name, spec in TOOLS[kind].items()
    ]


def _arg_schema(shape):
    if isinstance(shape, tuple):
        return {"type": "string", "enum": list(shape)}
    if shape == STR:
        return {"type": "string", "minLength": 1}
    if shape == FINDINGS:
        finding = {"type": "object", "additionalProperties": False,
                   "required": list(FINDING_FIELDS),
                   "properties": {f: {"type": "string"} for f in FINDING_FIELDS}}
        return {"type": "array", "items": finding}
    if shape == COVERS:
        return {"type": "object", "additionalProperties": {
            "type": "array", "items": {"type": "string"}, "minItems": 1}}
    return {"type": "array", "items": {"type": "string"},
            **({"minItems": 1} if shape == ARGV else {})}


def json_schema(kind):
    """A JSON Schema for one tool call of this kind, for model CLIs that
    enforce a schema while the model writes. Agent CLIs refuse a schema
    whose root is not an object, so `tool` and `args` are not tied to
    each other here; parse() still checks the pairing."""
    return {
        "type": "object", "additionalProperties": False, "required": ["tool", "args"],
        "properties": {
            "tool": {"type": "string", "enum": sorted(TOOLS[kind])},
            "args": {"anyOf": [
                {"type": "object", "additionalProperties": False, "required": list(spec),
                 "properties": {k: _arg_schema(v) for k, v in spec.items()}}
                for spec in TOOLS[kind].values()
            ]},
        },
    }
