"""Safe execution (Phase 3).

What the core enforces, deterministically:
- Forbidden bypass flags are rejected before any command launches.
- Credentials and git-redirecting variables are stripped from every
  subprocess environment.
- Verification commands (red / gate / replay / probe) run with an
  empty verify-home and, where the OS supports it, under a Landlock
  sandbox: worktree + verify-home writable, system dirs read-only,
  all TCP denied.

What it does NOT claim: on a host with no sandbox adapter, nothing
above stops a subprocess from reaching the network or the rest of
the filesystem. The safety probes measure that gap instead of
asserting it away — attempt the access, verify it is denied, and
record the result as evidence. `local-verified` never claims
isolation; the manifest lists the adapter and the probe results.
"""

import ctypes
import os
import platform
import re
import shutil
import struct
import subprocess

from . import evidence, store
from .errors import RunnerError, StateError

# ---------------------------------------------------------------- flags

FORBIDDEN_BYPASS_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--no-sandbox",
    "--bypass-sandbox",
})


def check_command(argv):
    """Reject permission-bypass flags. Token-based: a flag must be a
    whole token, or `flag=value`. Substrings inside other tokens
    (paths, code strings) do not trigger."""
    for token in argv:
        lower = str(token).strip().lower()
        for flag in FORBIDDEN_BYPASS_FLAGS:
            if lower == flag or lower.startswith(flag + "="):
                raise RunnerError(
                    f"permission bypass flag {token!r} is forbidden by factory policy"
                )


# ---------------------------------------------------------------- env

FORBIDDEN_ENV_EXACT = frozenset({
    "ASANA_ACCESS_TOKEN", "ASANA_PAT", "ASANA_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "ANTHROPIC_API_KEY", "DATABASE_URL", "GITHUB_TOKEN", "GH_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS", "OPENAI_API_KEY",
    "PRODUCTION_DATABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
    "STRIPE_SECRET_KEY", "SLACK_BOT_TOKEN", "TURBO_GITHUB_TOKEN",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS", "SSH_ASKPASS",
    "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
})

FORBIDDEN_ENV_PATTERNS = (
    re.compile(r"(^|_)(PROD|PRODUCTION)(_|$)", re.IGNORECASE),
    re.compile(r"(^|_)(SECRET|PASSWORD|PRIVATE_KEY|ACCESS_TOKEN|AUTH_TOKEN|API_KEY|PAT|TOKEN)(_|$)", re.IGNORECASE),
    re.compile(r"^(AWS_|GITHUB_|GH_|ASANA_|STRIPE_|SLACK_|TWILIO_|SENDGRID_|SSH_|GIT_CONFIG_)", re.IGNORECASE),
    re.compile(r"^TURBO_.*_URL$", re.IGNORECASE),
)


def is_forbidden_env_key(key):
    upper = key.upper()
    return upper in FORBIDDEN_ENV_EXACT or any(p.search(upper) for p in FORBIDDEN_ENV_PATTERNS)


# ---------------------------------------------------------------- verify-home

def verification_env(home, task_id):
    """Empty home for verification commands: no credential stores, no
    global git config, temp files inside the state home. The XDG
    directories move with HOME: a host XDG_CONFIG_HOME would point git
    at the operator's real config, which the cage then denies, and git
    treats an unreadable config as fatal."""
    verify_home = os.path.join(home, "verify-home", task_id)
    store.mkdir_private(verify_home)
    env = {"HOME": verify_home, "GIT_CONFIG_NOSYSTEM": "1"}
    for key, sub in (("TMPDIR", ".tmp"), ("XDG_CONFIG_HOME", ".config"),
                     ("XDG_CACHE_HOME", ".cache"), ("XDG_DATA_HOME", ".local/share"),
                     ("XDG_STATE_HOME", ".local/state")):
        env[key] = os.path.join(verify_home, sub)
        store.mkdir_private(env[key])
    return env


# ---------------------------------------------------------------- Landlock

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_RW = (1 << 16) - 1
_FS_RESOLVE_UNIX = 1 << 16  # connect to a pathname unix socket; ABI >= 9
_FS_RO_DIR = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
_FS_RO_FILE = _FS_EXECUTE | _FS_READ_FILE
_FS_RW_FILE = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE
_NET_TCP = 3  # bind + connect
_SCOPE_ALL = 3  # abstract unix sockets + signals leaving the cage; ABI >= 6

_SYSTEM_RO_DIRS = (
    "/usr", "/usr/local", "/bin", "/sbin", "/lib", "/lib64",
    "/etc", "/proc", "/sys", "/opt",
)
_DEV_RW_FILES = (
    "/dev/null", "/dev/zero", "/dev/full", "/dev/random",
    "/dev/urandom", "/dev/tty",
)


def landlock_abi():
    """This kernel's Landlock ABI version, 0 when it has none."""
    if platform.system() != "Linux":
        return 0
    if platform.machine() not in ("x86_64", "aarch64"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return max(0, int(libc.syscall(_LANDLOCK_CREATE_RULESET, None, 0, 1)))
    except Exception:
        return 0


def landlock_available():
    """True when this Linux kernel can deny TCP via Landlock (ABI >= 4)."""
    return landlock_abi() >= 4


def detect_adapter():
    if landlock_available():
        return "landlock"
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    return "none"


# ---------------------------------------------------------------- Docker

DEFAULT_DOCKER_IMAGE = "python:3.12-slim"


def docker_available():
    """True when a docker binary exists and its daemon answers."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def docker_image_present(image):
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_adapter(task):
    """(adapter, image) for a task. The request comes from the task
    file's `sandbox_adapter` / `sandbox_image`, else the
    T12_SANDBOX_ADAPTER / T12_SANDBOX_IMAGE environment, else auto.
    An explicit docker request is fail-closed at admission: the
    daemon must answer and the image must already be present
    locally — no implicit pulls mid-run. An explicit landlock
    request fails closed when the kernel cannot do it."""
    requested = (task.get("sandbox_adapter")
                 or os.environ.get("T12_SANDBOX_ADAPTER") or "auto")
    image = (task.get("sandbox_image")
             or os.environ.get("T12_SANDBOX_IMAGE") or DEFAULT_DOCKER_IMAGE)
    if requested == "docker":
        if not docker_available():
            raise StateError(
                "sandbox_adapter 'docker' requested but Docker is unavailable "
                "(no docker binary or the daemon does not answer)")
        if not docker_image_present(image):
            raise StateError(
                f"docker image {image!r} is not present locally; "
                f"pull it first: docker pull {image}")
        return "docker", image
    if requested == "landlock":
        if not landlock_available():
            raise StateError(
                "sandbox_adapter 'landlock' requested but this kernel cannot do it")
        return "landlock", None
    if requested == "auto":
        return detect_adapter(), None
    raise StateError(
        f"unknown sandbox_adapter {requested!r}; use 'docker', 'landlock', or 'auto'")


def docker_run_argv(image, worktree, verify_home, env, argv, read_only=()):
    """`docker run` argv for one verification command. The container
    gets no network (not even to the host), the worktree and the
    verify-home mounted at their host paths, the host user's uid/gid
    (so files it creates in the worktree are owned by the host user),
    and only the verification env — never the host's environment, so
    no secret can ride in through `-e`. The command itself is the
    trailing argv, never a shell string."""
    docker_argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{worktree}:{worktree}:rw",
        "-v", f"{verify_home}:{verify_home}:rw",
        *[arg for path in read_only for arg in ("-v", f"{path}:{path}:ro")],
        "-w", worktree,
        "--user", f"{os.getuid()}:{os.getgid()}",
    ]
    for key in sorted(env):
        docker_argv += ["-e", f"{key}={env[key]}"]
    docker_argv += [image, *[str(a) for a in argv]]
    return docker_argv


def landlock_preexec(worktree, verify_home, read_only=()):
    """Build a preexec function that cages the child: worktree and
    verify-home writable, system dirs read-only, all TCP denied.
    Raises OSError in the child if the cage cannot be built, which
    the parent reports as a launch failure (fail closed)."""
    return _landlock_preexec((worktree, verify_home), read_only,
                             handled_fs=_FS_RW, handled_net=_NET_TCP, scoped=0)


def builder_cage_preexec(writable, read_only=(), network=False):
    """Build a preexec function that cages a builder: only `writable`
    can be written, system dirs and `read_only` can be read, and TCP
    is denied unless `network`. Where the kernel can, the child also
    cannot signal a process outside the cage or connect to a unix
    socket one listens on (the session bus, a container daemon).
    Fails closed like `landlock_preexec`."""
    abi = landlock_abi()
    return _landlock_preexec(
        writable, read_only,
        handled_fs=_FS_RW | (_FS_RESOLVE_UNIX if abi >= 9 else 0),
        handled_net=0 if network else _NET_TCP,
        scoped=_SCOPE_ALL if abi >= 6 else 0)


def _landlock_preexec(writable, read_only, handled_fs, handled_net, scoped):
    writable = [os.path.realpath(path) for path in writable]

    def _preexec():
        libc = ctypes.CDLL(None, use_errno=True)
        attr = struct.pack("<QQQ", handled_fs, handled_net, scoped)
        ruleset_fd = int(libc.syscall(
            _LANDLOCK_CREATE_RULESET, attr, len(attr), 0))
        if ruleset_fd < 0:
            raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

        def _add_rule(path, access):
            fd = os.open(path, getattr(os, "O_PATH", 0o10000000) | os.O_CLOEXEC)
            try:
                rule = struct.pack("<Qii", access, fd, 0)
                rc = int(libc.syscall(
                    _LANDLOCK_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, rule, 0))
                if rc != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)

        try:
            for path in _SYSTEM_RO_DIRS:
                if os.path.exists(path):
                    _add_rule(path, _FS_RO_DIR)
            for path in _DEV_RW_FILES:
                if os.path.exists(path):
                    _add_rule(path, _FS_RW_FILE)
            if os.path.isdir("/dev"):
                _add_rule("/dev", _FS_RO_DIR)
            for path in writable:
                _add_rule(path, _FS_RW)
            for path in read_only:
                _add_rule(path, _FS_RO_DIR if os.path.isdir(path) else _FS_RO_FILE)
            if int(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) != 0:
                raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
            if int(libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)) != 0:
                raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
        finally:
            os.close(ruleset_fd)

    return _preexec


# ---------------------------------------------------------------- probes

REQUIRED_SAFETY_PROBES = ("forbidden_credential_access", "external_effect_restriction")

_CREDENTIAL_PROBE = """
import os, sys
sentinel = sys.argv[1]
found = []
for key in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "T12_PROBE_SECRET"):
    if os.environ.get(key):
        found.append("env:" + key)
for path in (sentinel, os.path.join(os.path.expanduser("~"), ".aws", "credentials")):
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        found.append("file:" + path)
    except OSError:
        pass
if found:
    print("GRANTED:" + ",".join(found))
    sys.exit(1)
print("DENIED")
sys.exit(0)
"""

_EXTERNAL_PROBE = """
import os, socket, sys
outside = sys.argv[1]
granted = []
try:
    socket.create_connection(("8.8.8.8", 53), timeout=3)
    granted.append("network")
except OSError:
    pass
try:
    with open(outside, "w") as fh:
        fh.write("x")
    granted.append("outside-write")
    os.remove(outside)
except OSError:
    pass
if granted:
    print("GRANTED:" + ",".join(granted))
    sys.exit(1)
print("DENIED")
sys.exit(0)
"""


def probe_commands(sentinel_path, outside_path):
    return [
        ("forbidden_credential_access",
         ["python3", "-c", _CREDENTIAL_PROBE, sentinel_path]),
        ("external_effect_restriction",
         ["python3", "-c", _EXTERNAL_PROBE, outside_path]),
    ]


def probes_passed(probes):
    return bool(probes) and all(p["passed"] for p in probes)


def run_probes(home, run, execute):
    """Attempt the forbidden accesses and record whether each was
    denied. `execute` is the command runner (runner.run). A probe
    passes when the access is denied; a granted access fails the
    probe and is recorded as evidence, not hidden."""
    worktree = run["worktree"]
    venv = verification_env(home, run["task_id"])
    sentinel = os.path.join(home, "probe-sentinel.txt")
    store.atomic_write(sentinel, b"t12-probe-sentinel\n")
    # The outside-write target must be outside every cage: the state
    # home root is not a Landlock-writable path and is not mounted
    # into a Docker container, while /tmp is the container's own
    # ephemeral /tmp under Docker (writable, and therefore not a
    # test of confinement at all).
    outside = os.path.join(home, "probe-outside.txt")
    adapter = run.get("sandbox_adapter") or detect_adapter()
    sandbox_ctx = {"worktree": worktree, "verify_home": venv["HOME"],
                   "adapter": adapter, "image": run.get("sandbox_image")}
    os.environ["T12_PROBE_SECRET"] = "canary"
    try:
        results = []
        for name, argv in probe_commands(sentinel, outside):
            obs = execute(argv, cwd=worktree, timeout_s=15, env=venv,
                          sandbox=sandbox_ctx)
            detail = ""
            combined = (obs["stdout"] + obs["stderr"]).strip()
            if combined:
                detail = combined.splitlines()[-1][:200]
            if obs["timed_out"]:
                detail = "timed out"
            passed = obs["exit_code"] == 0 and "DENIED" in obs["stdout"]
            ref = evidence.write_observation(
                home, run["task_id"], f"probe-{name}-output.txt", obs)
            results.append({
                "name": name, "passed": passed, "detail": detail,
                "output": ref,
            })
        summary = {
            "adapter": adapter,
            "results": [
                {"name": r["name"], "passed": r["passed"], "detail": r["detail"]}
                for r in results
            ],
        }
        evidence.write_evidence(
            home, run["task_id"], "probes.json",
            store.canonical_json(summary))
        return results
    finally:
        os.environ.pop("T12_PROBE_SECRET", None)
