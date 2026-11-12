"""Tests for safe execution (Phase 3): forbidden flags, credential
stripping, the Landlock cage, verify-home, and safety probes.
Enforcement tests run only where Landlock is available; elsewhere
they skip, and the probe tests assert the gap is recorded, not hidden.
"""

import json
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest import mock

from t12 import runner, sandbox
from t12.errors import RunnerError, StateError


def _loopback_denied():
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
    except PermissionError:
        return True
    return False


CAGED = "binding loopback TCP is denied here, as inside a t12 verification cage"
LOOPBACK_DENIED = _loopback_denied()


class FlagTest(unittest.TestCase):
    def test_forbidden_flags_blocked(self):
        for argv in (
            ["agy", "--dangerously-skip-permissions"],
            ["agy", "--no-sandbox"],
            ["agy", "--bypass-sandbox"],
            ["agy", "--dangerously-skip-permissions=true"],
            ["agy", "--Dangerously-Skip-Permissions"],
        ):
            with self.assertRaises(RunnerError, msg=" ".join(argv)):
                sandbox.check_command(argv)

    def test_legitimate_commands_pass(self):
        for argv in (
            ["git", "status", "--porcelain"],
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"],
            ["python3", "-c", "print('hello')"],
            ["npm", "test"],
        ):
            sandbox.check_command(argv)  # must not raise

    def test_runner_refuses_forbidden_command(self):
        with self.assertRaises(RunnerError):
            runner.run(["echo", "--no-sandbox"], cwd="/tmp", timeout_s=5)


class EnvTest(unittest.TestCase):
    def test_forbidden_keys(self):
        for key in ("GITHUB_TOKEN", "MY_TOKEN", "AWS_SECRET_ACCESS_KEY",
                    "DATABASE_URL", "PYTHONPATH", "OPENAI_API_KEY",
                    "PRODUCTION_DATABASE_URL", "SSH_AUTH_SOCK", "GIT_DIR"):
            self.assertTrue(sandbox.is_forbidden_env_key(key), key)
        for key in ("PATH", "HOME", "USER", "LANG", "MY_SETTING"):
            self.assertFalse(sandbox.is_forbidden_env_key(key), key)

    def test_sanitized_env_strips_secrets(self):
        planted = {
            "GITHUB_TOKEN": "ghp_x",
            "T12_PROBE_SECRET": "canary",
            "MY_API_KEY": "k",
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        old = dict(os.environ)
        try:
            os.environ.update(planted)
            clean = runner.sanitized_env()
            for key in ("GITHUB_TOKEN", "T12_PROBE_SECRET", "MY_API_KEY"):
                self.assertNotIn(key, clean)
            self.assertEqual(clean["GIT_ALLOW_PROTOCOL"], "none")
            self.assertEqual(clean["PATH"], planted["PATH"])
        finally:
            os.environ.clear()
            os.environ.update(old)


class VerifyHomeTest(unittest.TestCase):
    def test_verify_home_is_empty_and_private(self):
        with tempfile.TemporaryDirectory() as home:
            env = sandbox.verification_env(home, "demo-1")
            self.assertTrue(env["HOME"].startswith(home))
            self.assertTrue(os.path.isdir(env["HOME"]))
            self.assertTrue(os.path.isdir(env["TMPDIR"]))
            self.assertEqual(oct(os.stat(env["HOME"]).st_mode & 0o777), "0o700")
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")


@unittest.skipUnless(sandbox.landlock_available(), "Landlock unavailable")
class LandlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worktree = os.path.join(self.tmp.name, "wt")
        self.verify_home = os.path.join(self.tmp.name, "vh")
        os.makedirs(self.worktree)
        os.makedirs(self.verify_home)
        self.ctx = {"worktree": self.worktree, "verify_home": self.verify_home}

    def tearDown(self):
        self.tmp.cleanup()

    def test_worktree_readable_and_writable(self):
        with open(os.path.join(self.worktree, "a.txt"), "w") as fh:
            fh.write("hello\n")
        obs = runner.run(
            ["python3", "-c", "print(open('a.txt').read().strip())"],
            cwd=self.worktree, timeout_s=10, sandbox=self.ctx)
        self.assertEqual(obs["exit_code"], 0)
        self.assertEqual(obs["stdout"].strip(), "hello")
        self.assertEqual(obs["sandbox"], "landlock")

    def test_write_outside_worktree_denied(self):
        target = os.path.join(self.tmp.name, "outside.txt")
        obs = runner.run(
            ["python3", "-c", f"open({target!r}, 'w').write('x')"],
            cwd=self.worktree, timeout_s=10, sandbox=self.ctx)
        self.assertNotEqual(obs["exit_code"], 0)
        self.assertIn("PermissionError", obs["stderr"])
        self.assertFalse(os.path.exists(target))

    @unittest.skipIf(LOOPBACK_DENIED, CAGED)
    def test_localhost_tcp_denied_but_allowed_without_sandbox(self):
        server = socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        code = (
            "import socket; socket.create_connection(('127.0.0.1', %d), timeout=3);"
            " print('CONNECTED')" % port
        )
        try:
            denied = runner.run(["python3", "-c", code], cwd=self.worktree,
                                timeout_s=10, sandbox=self.ctx)
            self.assertNotEqual(denied["exit_code"], 0)
            self.assertEqual(denied["sandbox"], "landlock")
            allowed = runner.run(["python3", "-c", code], cwd=self.worktree,
                                 timeout_s=10)
            self.assertEqual(allowed["exit_code"], 0)
            self.assertIn("CONNECTED", allowed["stdout"])
        finally:
            server.server_close()


class ProbeTest(unittest.TestCase):
    def test_probes_pass_under_landlock(self):
        if not sandbox.landlock_available():
            self.skipTest("Landlock unavailable; probes measure the gap")
        with tempfile.TemporaryDirectory() as home:
            worktree = os.path.join(home, "wt")
            os.makedirs(worktree)
            run = {"task_id": "probe-1", "worktree": worktree}
            results = sandbox.run_probes(home, run, runner.run)
            self.assertEqual(
                [r["name"] for r in results],
                list(sandbox.REQUIRED_SAFETY_PROBES))
            for result in results:
                self.assertTrue(result["passed"], result)
            self.assertTrue(sandbox.probes_passed(results))
            probes_json = os.path.join(home, "evidence", "probe-1", "probes.json")
            self.assertTrue(os.path.exists(probes_json))
            with open(probes_json) as fh:
                summary = json.load(fh)
            self.assertEqual(summary["adapter"], "landlock")
            self.assertNotIn("T12_PROBE_SECRET", os.environ)

    def test_probes_passed_requires_all(self):
        self.assertFalse(sandbox.probes_passed([]))
        self.assertFalse(sandbox.probes_passed([
            {"name": "a", "passed": True}, {"name": "b", "passed": False}]))
        self.assertTrue(sandbox.probes_passed([
            {"name": "a", "passed": True}, {"name": "b", "passed": True}]))


class DockerTest(unittest.TestCase):
    def test_docker_run_argv(self):
        argv = sandbox.docker_run_argv(
            "python:3.12-slim", "/wt", "/vh",
            {"HOME": "/vh", "TMPDIR": "/vh/.tmp", "GIT_CONFIG_NOSYSTEM": "1"},
            ["python3", "-m", "unittest", "tests.test_greet", "-q"])
        self.assertEqual(argv[:5], ["docker", "run", "--rm", "--network", "none"])
        self.assertIn("-v", argv)
        self.assertIn("/wt:/wt:rw", argv)
        self.assertIn("/vh:/vh:rw", argv)
        self.assertIn("-w", argv)
        self.assertEqual(argv[argv.index("-w") + 1], "/wt")
        self.assertEqual(argv[argv.index("--user") + 1],
                         f"{os.getuid()}:{os.getgid()}")
        self.assertIn("-e", argv)
        self.assertIn("HOME=/vh", argv)
        self.assertNotIn("GITHUB_TOKEN", " ".join(argv))
        self.assertEqual(argv[-6:], ["python:3.12-slim", "python3", "-m",
                                     "unittest", "tests.test_greet", "-q"])

    def test_resolve_adapter_docker(self):
        with mock.patch.object(sandbox, "docker_available", return_value=True), \
                mock.patch.object(sandbox, "docker_image_present", return_value=True):
            adapter, image = sandbox.resolve_adapter({"sandbox_adapter": "docker"})
            self.assertEqual((adapter, image), ("docker", "python:3.12-slim"))
            adapter, image = sandbox.resolve_adapter(
                {"sandbox_adapter": "docker", "sandbox_image": "node:22-slim"})
            self.assertEqual((adapter, image), ("docker", "node:22-slim"))

    def test_resolve_adapter_docker_fail_closed(self):
        with mock.patch.object(sandbox, "docker_available", return_value=False):
            with self.assertRaises(StateError):
                sandbox.resolve_adapter({"sandbox_adapter": "docker"})
        with mock.patch.object(sandbox, "docker_available", return_value=True), \
                mock.patch.object(sandbox, "docker_image_present", return_value=False):
            with self.assertRaises(StateError):
                sandbox.resolve_adapter({"sandbox_adapter": "docker"})
        with self.assertRaises(StateError):
            sandbox.resolve_adapter({"sandbox_adapter": "bogus"})

    def test_resolve_adapter_env_override_and_auto(self):
        with mock.patch.object(sandbox, "docker_available", return_value=True), \
                mock.patch.object(sandbox, "docker_image_present", return_value=True), \
                mock.patch.dict(os.environ, {"T12_SANDBOX_ADAPTER": "docker"}):
            self.assertEqual(sandbox.resolve_adapter({})[0], "docker")
        adapter, image = sandbox.resolve_adapter({})
        self.assertEqual(adapter, "landlock" if sandbox.landlock_available() else "none")
        self.assertIsNone(image)


if __name__ == "__main__":
    unittest.main()
