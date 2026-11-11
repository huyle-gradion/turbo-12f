"""Durable storage: atomic writes, private permissions, hash, and
symlink/path checks. Evidence is re-read and re-hashed before any
certification; a file that moved, or a link that escapes the state
home, fails closed.
"""

import hashlib
import json
import os
import tempfile

from .errors import EvidenceError


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def ensure_inside(path, home):
    """Resolve path and require it to live under home; reject symlinks."""
    home = os.path.realpath(home)
    if os.path.islink(path):
        raise EvidenceError(f"refusing symlinked path: {path}")
    real = os.path.realpath(path)
    if os.path.commonpath([real, home]) != home:
        raise EvidenceError(f"path escapes the state home: {path}")
    return real


def atomic_write(path, data: bytes, mode=0o600):
    """Write via a temp file in the same directory, then rename."""
    path = str(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".t12-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_json(path, obj):
    atomic_write(path, canonical_json(obj) + b"\n")


def load_json(path):
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode())


def mkdir_private(path):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)
