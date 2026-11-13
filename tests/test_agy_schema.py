import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from adapters import runner


class AgySchemaTest(unittest.TestCase):
    def test_agy_schema_and_unknown_program(self):
        cmd = ["agy", "-p", "do the thing"]
        cli = runner.schema_cli(cmd)
        self.assertEqual(cli, "agy")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "schema.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"type": "object"}')
            argv = runner.with_schema(cmd, cli, path, os.path.join(d, "out"))
        # agy refuses --json-schema unless --output-format is json.
        self.assertEqual(argv[:5], ["agy", "--output-format", "json",
                                    "--json-schema", '{"type": "object"}'])
        self.assertEqual(argv[5], "-p")
        self.assertEqual(argv[-1], "do the thing")
        self.assertIsNone(runner.schema_cli(["gemini", "-p", "x"]))

    def test_an_agy_envelope_yields_its_structured_output_and_response_text(self):
        envelope = ('{"conversation_id": "c", "status": "SUCCESS", "response": "ok text", '
                    '"structured_output": {"tool": "escalate", "args": {"question": "q"}}}')
        call, text = runner.structured_result("agy", envelope, "/nonexistent")
        self.assertEqual(json.loads(call), {"tool": "escalate", "args": {"question": "q"}})
        self.assertEqual(text, "ok text")

    def test_an_agy_reply_without_structured_output_falls_back_to_its_text(self):
        for envelope in ('{"status": "SUCCESS", "response": "plain"}', "not json at all"):
            call, text = runner.structured_result("agy", envelope, "/nonexistent")
            self.assertIsNone(call)
            self.assertEqual(text, "plain" if envelope.startswith("{") else envelope)


if __name__ == "__main__":
    unittest.main()
