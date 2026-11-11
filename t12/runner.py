"""Safe command execution. Factor 8: deterministic code runs tools.

Rules: argv arrays only, never shell strings; timeouts enforced and
the whole process group killed on expiry; stdout/stderr captured;
duration and exit code recorded; credentials stripped from the
environment. A launch failure is an explicit RunnerError, never a
silent empty result.
"""

import os
import signal
import subprocess
import time

from . import sandbox as sandbox_module
from .errors import RunnerError


def sanitized_env(extra=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not sandbox_module.is_forbidden_env_key(k)
    }
    env["GIT_ALLOW_PROTOCOL"] = "none"
    if extra:
        env.update(extra)
    return env


def run(argv, cwd, timeout_s, env=None, sandbox=None):
    """Run argv (a list) and return an observation dict.

    Never raises for a non-zero exit: that is data. Raises RunnerError
    only when the command cannot be launched at all. Forbidden
    bypass flags are rejected before launch. When `sandbox` is
    given ({"worktree", "verify_home"}), verification commands run
    under the Landlock cage where the OS supports it; the
    observation records which adapter applied ("landlock" or "none").
    """
    if isinstance(argv, (str, bytes)):
        raise RunnerError("argv must be a list, never a shell string")
    sandbox_module.check_command(argv)
    argv = [str(a) for a in argv]
    started = time.monotonic()
    kwargs = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": sanitized_env(env),
        "start_new_session": True,
    }
    sandbox_used = "none"
    if sandbox is not None:
        if sandbox_module.landlock_available():
            kwargs["preexec_fn"] = sandbox_module.landlock_preexec(
                sandbox["worktree"], sandbox["verify_home"])
            sandbox_used = "landlock"
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as exc:
        raise RunnerError(f"cannot launch {' '.join(argv)}: {exc}") from exc
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": stdout or "",
        "stderr": stderr or "",
        "sandbox": sandbox_used,
    }
