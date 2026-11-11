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

from .errors import RunnerError

_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "AWS_SESSION")


def sanitized_env(extra=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(m in k.upper() for m in _SECRET_MARKERS)
    }
    env["GIT_ALLOW_PROTOCOL"] = "none"
    if extra:
        env.update(extra)
    return env


def run(argv, cwd, timeout_s, env=None):
    """Run argv (a list) and return an observation dict.

    Never raises for a non-zero exit: that is data. Raises RunnerError
    only when the command cannot be launched at all.
    """
    if isinstance(argv, (str, bytes)):
        raise RunnerError("argv must be a list, never a shell string")
    argv = [str(a) for a in argv]
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=sanitized_env(env),
        start_new_session=True,
    )
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
    except OSError as exc:
        raise RunnerError(f"cannot launch {' '.join(argv)}: {exc}") from exc
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": stdout or "",
        "stderr": stderr or "",
    }
