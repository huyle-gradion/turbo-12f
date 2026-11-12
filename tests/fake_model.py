#!/usr/bin/env python3
"""A stand-in for `claude -p` in the runner tests. It reads the prompt on
stdin and acts by $T12_ROLE in its cwd, the run's worktree. FAKE_MODE
picks a misbehaviour: `escalate`, `garbage-once` (builder output is not
JSON until the prompt says it was rejected), `builder-fails`,
`no-commit` (the builder leaves its edit uncommitted), `wrong-fix`
(the builder commits a change that does not pass the red test),
`fenced` (every call wrapped in prose and a ```json fence) or `twice`
(every call printed twice). Called with `--json-schema`, as `claude`
is, it answers in claude's JSON envelope with the call in
`structured_output`. Every call is appended to $FAKE_LOG."""

import json
import os
import subprocess
import sys

APP = 'def greet(name="world"):\n    return f"Hello, {name}!"\n'
TEST = """import unittest
from app import greet

class TestGreet(unittest.TestCase):
    def test_named(self):
        self.assertEqual(greet("Ana"), "Hello, Ana!")

if __name__ == "__main__":
    unittest.main()
"""

prompt = sys.stdin.read()
role, mode = os.environ["T12_ROLE"], os.environ.get("FAKE_MODE", "")
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    schema = (json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
              if "--json-schema" in sys.argv else None)
    log.write(json.dumps({"role": role, "session": os.environ["T12_SESSION"],
                          "rejected": "previous output was rejected" in prompt,
                          "schema_tools": schema and schema["properties"]["tool"]["enum"]})
              + "\n")


def say(tool, **args):
    call = json.dumps({"tool": tool, "args": args})
    if schema:
        print(json.dumps({"type": "result", "result": f"Done.\n{call}",
                          "structured_output": {"tool": tool, "args": args}}))
    elif mode == "fenced":
        print(f"I did the work.\n\n```json\n{call}\n```")
    elif mode == "twice":
        print(f"{call}\n\n{call}")
    else:
        print(call)


if role == "test-writer":
    if mode == "escalate":
        say("escalate", question="Which greeting does the product want?")
    else:
        with open("tests/test_greet.py", "w", encoding="utf-8") as fh:
            fh.write(TEST)
        say("red_test_ready", test_path="tests/test_greet.py",
            command=["python3", "-m", "unittest", "tests.test_greet", "-q"], marker="FAILED",
            covers={"0": ["test_named"]})
elif role == "builder":
    if mode == "builder-fails":
        sys.exit("model unavailable")
    if mode == "garbage-once" and "previous output was rejected" not in prompt:
        print("Sure! Here is what I did.")
        sys.exit(0)
    with open("app.py", "w", encoding="utf-8") as fh:
        fh.write(APP.replace("Hello,", "Hi,") if mode == "wrong-fix" else APP)
    if mode == "no-commit":
        say("implementation_ready", summary="greet() takes a name")
        sys.exit(0)
    for cmd in (["add", "app.py"], ["commit", "-qm", "feat: greet by name"]):
        subprocess.run(["git", *cmd], check=True)
    say("implementation_ready", summary="greet() takes a name")
else:
    say("review", verdict="merge-ready", findings=[], verification="re-ran the red test")
