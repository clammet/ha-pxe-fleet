"""Pi-side A/B SD updates. Only the inactive slot and preallocated selector are writable."""
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.request

if __package__:
    from .sd_layout import FORMAT, SECTOR, SLOT_BYTES, STARTS, Fat, read_at, selected, selector, sync, validate_mbr, write_at
else:
    from fleet_sd_layout import FORMAT, SECTOR, SLOT_BYTES, STARTS, Fat, read_at, selected, selector, sync, validate_mbr, write_at

LOG = logging.getLogger("fleet-sd")
MAX_DOWNLOAD = 32 * 1024**2
RUNTIME = Path("/run")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sd-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            sync(stream)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def boot_identity(cmdline, tree):
    args = dict(word.split("=", 1) for word in cmdline.split() if "=" in word)
    if args.get("fleet.sd") != str(FORMAT):
        return None  # Native boot or a legacy card: do not inspect/write any disk.
    card_id, model = args.get("fleet.card", ""), args.get("fleet.sd_model", "")
    if not re.fullmatch(r"[a-f0-9]{32}", card_id) or model not in ("pi1", "pi2", "pi3", "pi3plus", "pi4"):
        raise ValueError("Invalid Fleet SD boot identity")
    partition = int.from_bytes((tree / "chosen/bootloader/partition").read_bytes(), "big")
    trial = int.from_bytes((tree / "chosen/bootloader/tryboot").read_bytes(), "big")
    if partition not in STARTS or trial not in (0, 1) or int(args.get("fleet.slot", "0"), 16) != partition:
        raise ValueError("Firmware boot partition does not match the SD loader")
    serial = (tree / "serial-number").read_bytes().rstrip(b"\0").decode().lower()[-8:]
    return {"format": FORMAT, "card_id": card_id, "model": model, "serial": serial,
            "partition": partition, "tryboot": bool(trial)}


def check_description(description, identity, desired):
    if (description.get("format") != FORMAT or description.get("model") != identity["model"]
            or description.get("serial") != identity["serial"] or description.get("generation") != desired
            or description.get("raw_size") != SLOT_BYTES
            or type(description.get("size")) is not int or not 0 < description["size"] <= MAX_DOWNLOAD):
        raise ValueError("SD update is not for this card/generation")
    for field in ("revision", "sha256", "raw_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(description.get(field, ""))):
            raise ValueError("Invalid SD update digest")


class Card:
    """Bounded media operations; takes a stream so fault tests never need a disk."""
    def __init__(self, stream, identity):
        self.stream, self.identity = stream, identity
        validate_mbr(stream, identity["card_id"])
        self.recovery = Fat(stream, STARTS[1] * SECTOR)
        marker = self.recovery.metadata("FLEET   ID ")
        if marker != {k: identity[k] for k in ("format", "card_id", "model", "serial")}:
            raise ValueError("SD card marker/serial does not match this boot")
        self.selector_offset = self.recovery.selector_offset()

    def active(self):
        return selected(read_at(self.stream, self.selector_offset, SECTOR))

    def revision(self, part):
        if part not in STARTS:
            raise ValueError("Invalid SD partition")
        marker = Fat(self.stream, STARTS[part] * SECTOR).metadata()
        if marker.get("model") != self.identity["model"] or marker.get("serial") != self.identity["serial"]:
            raise ValueError("SD slot belongs to another board")
        revision = marker.get("revision", "")
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("Invalid SD slot revision")
        return revision

    def install(self, archive, description, destination):
        if destination not in (2, 3) or destination in (self.active(), self.identity["partition"]):
            raise ValueError("Refusing to overwrite an active/recovery SD slot")
        # Validate the complete expanded image before opening the write phase.
        with gzip.open(archive, "rb") as source:
            raw_hash, size = hashlib.sha256(), 0
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > SLOT_BYTES:
                    raise ValueError("Expanded SD image is oversized")
                raw_hash.update(block)
        if size != SLOT_BYTES or raw_hash.hexdigest() != description["raw_sha256"]:
            raise ValueError("Expanded SD image checksum mismatch")
        with gzip.open(archive, "rb") as source:
            marker = Fat(source).metadata()
            if any(marker.get(k) != description[k] for k in ("format", "model", "serial", "revision")):
                raise ValueError("SD image metadata does not match its signed description")
        offset = STARTS[destination] * SECTOR
        with gzip.open(archive, "rb") as source:
            written = 0
            while block := source.read(1024 * 1024):
                if written + len(block) > SLOT_BYTES:
                    raise ValueError("SD image changed during installation")
                write_at(self.stream, offset + written, block)
                written += len(block)
        sync(self.stream)
        # Read back every byte before the one-shot reboot can be requested.
        actual = hashlib.sha256()
        for position in range(0, SLOT_BYTES, 1024 * 1024):
            actual.update(read_at(self.stream, offset + position, 1024 * 1024))
        if actual.hexdigest() != description["raw_sha256"] or self.revision(destination) != description["revision"]:
            raise OSError("SD image read-back verification failed")

    def commit(self, part, revision):
        if part not in (2, 3) or part != self.identity["partition"] or self.revision(part) != revision:
            raise ValueError("Only the running, verified SD slot may be committed")
        if self.active() == part:
            return False
        # No FAT allocation/directory changes: only one preallocated data sector.
        # The immutable recovery files remain available if this sector tears.
        write_at(self.stream, self.selector_offset, selector(part))
        sync(self.stream)
        if self.active() != part:
            raise OSError("SD selector read-back verification failed")
        return True


def busy(device):
    entries = [Path("/sys/class/block") / device.name,
               *Path("/sys/class/block").glob(device.name + "p[123]")]
    numbers = {p.joinpath("dev").read_text().strip() for p in entries}
    mounted = {line.split()[2] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    if numbers & mounted or any(list((p / "holders").iterdir()) for p in entries):
        raise RuntimeError("Refusing SD writes while a disk/partition is mounted or in use")
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        if line.split()[0].startswith(str(device)):
            raise RuntimeError("Refusing to write an SD card used for swap")


@contextmanager
def open_card(device, identity, writable=False):
    before = device.stat()
    if not stat.S_ISBLK(before.st_mode):
        raise ValueError("SD target is not a block device")
    if writable:
        busy(device)
    flags = os.O_NOFOLLOW | (os.O_RDWR | os.O_EXCL if writable else os.O_RDONLY)
    fd = os.open(device, flags)
    try:
        after = os.fstat(fd)
        if (before.st_rdev, before.st_ino) != (after.st_rdev, after.st_ino):
            raise ValueError("SD device changed while opening it")
        with os.fdopen(fd, "r+b" if writable else "rb", buffering=0, closefd=False) as stream:
            fcntl.flock(fd, fcntl.LOCK_EX if writable else fcntl.LOCK_SH)
            yield Card(stream, identity)
    finally:
        os.close(fd)


def find_card(identity):
    matches = []
    for path in Path("/sys/class/block").iterdir():
        if not re.fullmatch(r"mmcblk[0-9]+", path.name):
            continue
        if (path / "device/type").read_text().strip() != "SD":
            continue
        device = Path("/dev") / path.name
        try:
            with open_card(device, identity):
                matches.append(device)
        except (ValueError, OSError):
            continue
    if len(matches) != 1:
        raise RuntimeError("Cannot uniquely identify the SD card used for this boot")
    return matches[0]


def download(spec, description, destination):
    path = f"/v1/clients/{spec['serial']}/sd/{description['generation']}/{description['model']}"
    signature = hmac.new(spec["token"].encode(), ("GET " + path).encode(), hashlib.sha256).hexdigest()
    url = f"http://{spec['server_ip']}:{spec['control_port']}" + path
    request = urllib.request.Request(url, headers={"X-Fleet-Signature": signature})
    hasher, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
        if response.url != url:
            raise ValueError("SD update downloads must not redirect")
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > description["size"]:
                raise ValueError("SD update exceeds its signed size")
            target.write(block)
            hasher.update(block)
    if size != description["size"] or hasher.hexdigest() != description["sha256"]:
        raise ValueError("SD update download checksum mismatch")


class Manager:
    def __init__(self, spec, identity, device, state_path, boot_id):
        self.spec, self.identity, self.device = spec, identity, device
        self.state_path, self.boot_id = state_path, boot_id
        self.state = json.loads(state_path.read_text()) if state_path.exists() else {"failed": {}, "pending": None, "retry": 0}
        self.error = None
        with open_card(device, identity) as card:
            self.current = card.revision(identity["partition"])
            self.active = card.active()
        pending = self.state["pending"]
        self.trial = identity["tryboot"] and self.active != identity["partition"]
        if pending and pending["boot_id"] != boot_id:
            if self.current == pending["revision"] and identity["partition"] == pending["slot"]:
                if self.active == pending["slot"]:
                    # Power loss after selector commit but before NFS state save.
                    self.state["pending"] = None
                    save(self.state_path, self.state)
                    self.trial = False
            elif not self.trial:
                self.fail(pending["revision"], pending["generation"], "SD trial returned to the previous slot")
        if self.trial and self.state["pending"]:
            pending = self.state["pending"]
            if pending.get("trial_boot_id") != boot_id:
                pending.update(trial_boot_id=boot_id, trial_started=time.monotonic())
                save(self.state_path, self.state)

    def fail(self, revision, generation, message):
        self.state["failed"][revision] = generation
        self.state["failed"] = dict(list(self.state["failed"].items())[-32:])
        self.state["pending"] = None
        self.state["failure"] = generation
        self.error = message
        save(self.state_path, self.state)
        LOG.error("%s; revision %s is quarantined", message, revision)

    def report(self):
        return {**self.identity, "revision": self.current, "trial": self.trial,
                "retry": self.state["retry"], "failed_generation": self.state.get("failure"), "error": self.error}

    def expire_trial(self):
        pending = self.state["pending"]
        if self.trial and pending and time.monotonic() - pending["trial_started"] >= self.spec.get("boot_timeout_seconds", 900):
            self.fail(pending["revision"], pending["generation"], "SD trial did not finish before its deadline")
            return True
        return False

    def handle(self, reply, healthy):
        retry = reply.get("sd_retry", 0)
        if type(retry) is int and retry > self.state["retry"]:
            self.state.update(failed={}, failure=None, retry=retry)
            save(self.state_path, self.state)
        pending = self.state["pending"]
        if self.trial:
            if (not pending or self.current != pending["revision"] or
                    self.identity["partition"] != pending["slot"]):
                return "reboot"  # Never promote an untracked one-shot boot.
            if reply["desired"] != pending["generation"]:
                self.fail(pending["revision"], pending["generation"], "Controller rolled back during SD trial")
                return "reboot"
            if self.spec["generation"] != pending["generation"]:
                self.fail(pending["revision"], pending["generation"], "SD trial booted the wrong OS generation")
                return "reboot"
            if not healthy:
                if self.expire_trial():
                    return "reboot"
                return "wait"
            with open_card(self.device, self.identity, writable=True) as card:
                card.commit(pending["slot"], pending["revision"])
            self.active = pending["slot"]
            self.state.update(pending=None, failure=None)
            save(self.state_path, self.state)
            self.trial = False
            LOG.info("Confirmed SD firmware revision %s", self.current)
            return "ready"
        description = reply.get("sd_update")
        if pending and pending["boot_id"] == self.boot_id and reply["desired"] != pending["generation"]:
            self.state["pending"] = pending = None
            save(self.state_path, self.state)
        if not self.spec.get("sd_updates", True) or not description:
            return "ready"
        check_description(description, self.identity, reply["desired"])
        if description["revision"] == self.current:
            return "ready"
        if pending and pending["boot_id"] == self.boot_id:
            if (pending["revision"], pending["generation"]) == (description["revision"], reply["desired"]):
                return "tryboot"  # Reboot command failed/restarted agent: no second write.
            self.state["pending"] = None
            save(self.state_path, self.state)
        if description["revision"] in self.state["failed"]:
            # Also reject a later OS generation using the same failed firmware.
            if self.state.get("failure") != reply["desired"]:
                self.state["failure"] = reply["desired"]
                save(self.state_path, self.state)
            return "wait"
        if not healthy and not reply.get("reboot"):
            return "wait"
        destination = 5 - self.active
        if destination == self.identity["partition"]:
            raise ValueError("Cannot stage firmware over the running SD slot")
        with tempfile.TemporaryDirectory(prefix="fleet-sd-", dir=RUNTIME) as tmp:
            archive = Path(tmp) / "slot.img.gz"
            download(self.spec, description, archive)
            with open_card(self.device, self.identity, writable=True) as card:
                if card.active() != self.active:
                    raise ValueError("SD selector changed during update")
                card.install(archive, description, destination)
        self.state["pending"] = {"revision": description["revision"], "slot": destination,
                                 "generation": reply["desired"], "boot_id": self.boot_id}
        self.state["failure"] = None
        save(self.state_path, self.state)
        LOG.info("Staged SD firmware %s in partition %s", description["revision"], destination)
        return "tryboot"


def detect(spec, boot_id):
    identity = boot_identity(Path("/proc/cmdline").read_text(), Path("/proc/device-tree"))
    if identity is None:
        return None
    if identity["serial"] != spec["serial"] or ("pi3" if identity["model"] == "pi3plus" else identity["model"]) != spec["model"]:
        raise ValueError("SD card and deployed client configuration disagree")
    device = find_card(identity)
    return Manager(spec, identity, device, Path("/appdata/.fleet") / ("sd-" + identity["card_id"] + ".json"), boot_id)
