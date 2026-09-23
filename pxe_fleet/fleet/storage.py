"""Provision a per-client ext4 container disk whose backing file is served by NFS."""
import logging
import os
from pathlib import Path
import stat
import tempfile
import uuid

from .util import capture, digest, run

LOG = logging.getLogger(__name__)


def container_disk(data, client, token):
    if not client["containers"]:
        return
    state = data / ".fleet"
    if state.is_symlink():
        raise RuntimeError("Refusing a symlink for protected fleet storage")
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    destination = state / "podman.ext4"
    expected = str(uuid.UUID(hex=digest([client["serial"], token, "podman"])[0:32]))
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not stat.S_ISREG(destination.stat().st_mode):
            raise RuntimeError("Container backing disk must be a regular file")
        actual = dict(line.split("=", 1) for line in capture(["blkid", "-p", "-o", "export", destination]).splitlines() if "=" in line)
        if actual.get("TYPE") != "ext4" or actual.get("UUID") != expected:
            raise RuntimeError("Existing container disk has the wrong filesystem/identity; it was not modified")
        if destination.stat().st_size != client["container_storage_gib"] * 1024**3:
            LOG.warning("%s: container_storage_gib applies on creation only; retaining existing disk size", client["serial"])
        return
    fd, name = tempfile.mkstemp(prefix=".podman-", dir=state)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.truncate(client["container_storage_gib"] * 1024**3)
        run(["mkfs.ext4", "-q", "-F", "-U", expected, "-L", "fleet-podman",
             "-E", "lazy_itable_init=0,lazy_journal_init=0", temporary])
        run(["sync", "-f", temporary])
        temporary.replace(destination)
        run(["sync", "-f", state])
    finally:
        temporary.unlink(missing_ok=True)
