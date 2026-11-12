"""The tool-call schema the runner gives model CLIs, and how it finds
one tool call in a reply that is not only that call."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "adapters"))

from t12 import tools  # noqa: E402
import runner  # noqa: E402

CALL = {"tool": "implementation_ready", "args": {"summary": "done"}}
TEXT = json.dumps(CALL)


class Schema(unittest.TestCase):
    def test_every_tool_and_arg_of_a_kind_is_in_its_schema(self):
        for kind, specs in tools.TOOLS.items():
            schema = tools.json_schema(kind)
            self.assertEqual(schema["type"], "object")
            self.assertEqual(schema["properties"]["tool"]["enum"], sorted(specs))
            args = [sorted(a["required"]) for a in schema["properties"]["args"]["anyOf"]]
            self.assertEqual(sorted(args), sorted(sorted(spec) for spec in specs.values()))

    def test_an_enum_arg_is_closed_to_its_values(self):
        review = tools.json_schema("review")["properties"]["args"]["anyOf"]
        [args] = [a for a in review if "verdict" in a["properties"]]
        self.assertEqual(args["properties"]["verdict"]["enum"],
                         list(tools.TOOLS["review"]["review"]["verdict"]))


class Extract(unittest.TestCase):
    def test_a_call_in_a_fence_after_prose_is_found(self):
        self.assertEqual(json.loads(runner.extract_tool_call(
            f"I made the change.\n\n```json\n{TEXT}\n```\n")), CALL)

    def test_a_call_printed_twice_is_one_call(self):
        self.assertEqual(json.loads(runner.extract_tool_call(f"{TEXT}\n\n{TEXT}\n")), CALL)

    def test_a_bare_call_after_prose_is_found(self):
        self.assertEqual(json.loads(runner.extract_tool_call(f"Done.\n{TEXT}")), CALL)

    def test_two_different_calls_are_not_chosen_between(self):
        other = json.dumps({"tool": "escalate", "args": {"question": "which?"}})
        self.assertIsNone(runner.extract_tool_call(f"{TEXT}\n\n{other}"))

    def test_prose_alone_or_a_broken_fence_has_no_call(self):
        self.assertIsNone(runner.extract_tool_call("Sure! Here is what I did."))
        self.assertIsNone(runner.extract_tool_call('```json\n{"tool": "x", "args": \n```'))

    def test_a_clean_reply_is_raw_and_a_claude_envelope_is_schema(self):
        self.assertEqual(runner.tool_call(None, TEXT, None), (TEXT, "raw"))
        envelope = json.dumps({"result": "prose", "structured_output": CALL})
        payload, how = runner.tool_call("claude", envelope, None)
        self.assertEqual((json.loads(payload), how), (CALL, "schema"))

    def test_a_claude_reply_without_a_structured_result_falls_back_to_its_text(self):
        envelope = json.dumps({"result": f"Done.\n```json\n{TEXT}\n```"})
        payload, how = runner.tool_call("claude", envelope, None)
        self.assertEqual((json.loads(payload), how), (CALL, "extracted"))


if __name__ == "__main__":
    unittest.main()
