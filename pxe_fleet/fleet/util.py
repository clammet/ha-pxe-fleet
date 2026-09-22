"""Small durable filesystem and process primitives."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic_write(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, data, mode=0o600):
    atomic_write(path, canonical(data) + b"\n", mode)


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def capture(args):
    return run(args, stdout=subprocess.PIPE, text=True).stdout.strip()
