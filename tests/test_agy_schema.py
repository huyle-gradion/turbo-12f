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
        self.assertEqual(argv[:3], ["agy", "--json-schema", '{"type": "object"}'])
        self.assertEqual(argv[3], "-p")
        self.assertEqual(argv[-1], "do the thing")
        self.assertIsNone(runner.schema_cli(["gemini", "-p", "x"]))



if __name__ == "__main__":
    unittest.main()
