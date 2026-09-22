"""Durable rollout state, immutable generations, and atomic boot publication."""
import json
import logging
from pathlib import Path
import secrets
import shutil
import threading
import time

from .build import prepare_client, stage_applications
from .config import client_spec
from .util import atomic_write, digest, run, write_json

LOG = logging.getLogger(__name__)


def boot_config(client, generation):
    prefix = f"payloads/{generation}/"
    lines = ["[all]", "arm_64bit=1", "enable_uart=1", "auto_initramfs=0",
             "os_prefix=" + prefix, "kernel=fleet-kernel", "initramfs fleet-initrd followkernel"]
    if client["model"] != "pi5":
        suffix = "4" if client["model"] == "pi4" else ""
        lines.extend([f"start_file={prefix}start{suffix}.elf", f"fixup_file={prefix}fixup{suffix}.dat"])
    lines.extend(client["boot_options"])
    return "\n".join(lines) + "\n"


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        state = self.path / "state.json"
        self.state = json.loads(state.read_text()) if state.exists() else {"clients": {}}

    def save(self):
        write_json(self.path / "state.json", self.state)

    def register(self, cfg):
        with self.lock:
            for client in cfg["clients"]:
                serial = client["serial"]
                entry = self.state["clients"].setdefault(serial, {"token": secrets.token_hex(32), "active": None, "previous": None, "pending": None, "failed": [], "last_seen": None})
                # Old roots and data are retained when clients are removed from config.
                entry["client"] = client
                (self.path / "appdata" / serial).mkdir(parents=True, exist_ok=True)
            self.save()

    def root(self, serial, generation):
        return self.path / "generations" / serial / generation / "root"

    def nfs_root(self, serial, generation):
        return f"/{serial}/roots/{generation}"

    def desired(self, serial):
        entry = self.state["clients"][serial]
        return entry["pending"]["generation"] if entry["pending"] else entry["active"]

    def publish(self, serial, generation):
        entry = self.state["clients"][serial]
        manifest = json.loads((self.root(serial, generation).parent / "manifest.json").read_text())
        atomic_write(self.path / "tftp" / serial / "config.txt", boot_config(manifest["client"], generation))
        LOG.info("Boot target %s -> %s", serial, generation)

    def recover(self):
        # The state file is the journal. Reapply it after any interrupted publication.
        with self.lock:
            for serial in self.state["clients"]:
                desired = self.desired(serial)
                if desired:
                    self.publish(serial, desired)

    def stage(self, cfg, client, base, fingerprint):
        serial = client["serial"]
        spec = client_spec(cfg, client)
        generation = digest({"base": fingerprint, "spec": spec})[:24]
        with self.lock:
            entry = self.state["clients"][serial]
            if generation in entry["failed"] or generation == self.desired(serial) or entry["pending"]:
                return None
            token = entry["token"]
        final = self.root(serial, generation).parent
        if final.exists():
            self.prepare_boot(serial, generation)
            return generation
        stage = final.with_name(".stage-" + generation)
        if stage.exists():
            mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()] if Path("/proc/self/mountinfo").exists() else []
            if any(p == str(stage) or p.startswith(str(stage) + "/") for p in mounts):
                raise RuntimeError("Previous staging directory still has mounted filesystems")
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        try:
            root = stage / "root"
            run(["cp", "-a", "--reflink=auto", base, root])
            prepare_client(root, spec, generation, token, f"/{serial}/appdata")
            stage_applications(root, spec)
            write_json(stage / "manifest.json", {"client": client, "generation": generation, "base": fingerprint, "created": time.time()}, 0o644)
            boot = root / "boot/firmware"
            flavor = "2712" if client["model"] == "pi5" else "v8"
            shutil.copy2(boot / ("fleet-kernel-" + flavor), boot / "fleet-kernel")
            shutil.copy2(boot / ("fleet-initrd-" + flavor), boot / "fleet-initrd")
            cmdline = (f"console=serial0,115200 console=tty1 root=/dev/nfs boot=nfs "
                       f"nfsroot={cfg['server_ip']}:{self.nfs_root(serial, generation)},vers=4.1,proto=tcp,ro "
                       f"ip=dhcp rw rootwait panic=30 fleet.overlay={cfg['overlay_size']}\n")
            atomic_write(boot / "cmdline.txt", cmdline)
            # A complete generation must be on disk before either NFS or TFTP sees it.
            run(["sync", "-f", stage])
            stage.rename(final)
            self.prepare_boot(serial, generation)
        except BaseException:
            mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()] if Path("/proc/self/mountinfo").exists() else []
            mounted = any(p == str(stage) or p.startswith(str(stage) + "/") for p in mounts)
            if stage.exists() and not mounted:
                shutil.rmtree(stage)
            raise
        return generation

    def prepare_boot(self, serial, generation):
        boot = self.root(serial, generation) / "boot/firmware"
        target = self.path / "tftp" / serial / "payloads" / generation
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(".stage-" + generation)
            if temp.exists():
                shutil.rmtree(temp)
            shutil.copytree(boot, temp, symlinks=False)
            for path in temp.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            temp.rename(target)
        # Pi 3 ROM fetches bootcode.bin before it knows its serial prefix.
        bootcode = boot / "bootcode.bin"
        shared = self.path / "tftp/bootcode.bin"
        if bootcode.exists() and not shared.exists():
            atomic_write(shared, bootcode.read_bytes())

    def activate(self, serial, generation):
        with self.lock:
            entry = self.state["clients"][serial]
            if entry["pending"]:
                raise RuntimeError("A rollout is already pending")
            self.prepare_boot(serial, generation)
            entry["pending"] = {"generation": generation, "started": None}
            self.save()
            self.publish(serial, generation)

    def report(self, serial, status, timeout, now=None):
        now = time.time() if now is None else now
        with self.lock:
            entry = self.state["clients"][serial]
            entry["last_seen"] = now
            entry["reported"] = status
            pending = entry["pending"]
            if pending:
                if pending["started"] is None and (not status.get("updating") or status["generation"] == pending["generation"]):
                    pending["started"] = now
                if status["generation"] == pending["generation"] and status["healthy"]:
                    entry["previous"] = entry["active"]
                    entry["active"] = pending["generation"]
                    entry["pending"] = None
                    LOG.info("Client %s confirmed generation %s", serial, entry["active"])
                elif pending["started"] is not None and now - pending["started"] >= timeout:
                    self._rollback(serial)
            self.save()
            desired = self.desired(serial)
            return {"desired": desired, "reboot": bool(desired and desired != status["generation"] and not status.get("updating")), "nonce": status["nonce"]}

    def expire(self, timeout, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for serial, entry in self.state["clients"].items():
                pending = entry["pending"]
                if pending and pending["started"] is not None and now - pending["started"] >= timeout:
                    self._rollback(serial)

    def _rollback(self, serial):
        entry = self.state["clients"][serial]
        pending = entry["pending"]
        if not entry["active"]:
            # No known-good system exists on first deployment. Keep serving it so
            # a transient package outage can recover; never enter a reboot loop.
            LOG.error("First deployment for %s has not become healthy", serial)
            pending["started"] = None
            return
        entry["failed"].append(pending["generation"])
        entry["pending"] = None
        self.save()
        self.publish(serial, entry["active"])
        LOG.error("Rolled %s back to %s; app data was retained", serial, entry["active"])

    def prune_candidates(self, now=None):
        now = time.time() if now is None else now
        candidates = []
        with self.lock:
            for serial, entry in self.state["clients"].items():
                if not entry["active"]:
                    continue
                keep = {entry["active"], entry["previous"], self.desired(serial), entry.get("reported", {}).get("generation")}
                retired = entry.setdefault("retired", {})
                for manifest in (self.path / "generations" / serial).glob("*/manifest.json"):
                    generation = manifest.parent.name
                    if generation.startswith("."):
                        continue
                    if generation in keep:
                        retired.pop(generation, None)
                    elif now - retired.setdefault(generation, now) >= 7 * 86400:
                        candidates.append((serial, generation))
            self.save()
        return candidates

    def exports(self, excluded=()):
        pseudo = self.path / "nfs"
        ips = sorted({entry["client"]["ip"] for entry in self.state["clients"].values()})
        lines = [str(pseudo) + " " + " ".join(f"{ip}(ro,fsid=0,sync,root_squash,no_subtree_check,insecure)" for ip in ips)]
        for serial, entry in sorted(self.state["clients"].items()):
            ip = entry["client"]["ip"]
            roots = self.path / "generations" / serial
            for manifest in sorted(roots.glob("*/manifest.json")):
                if manifest.parent.name.startswith("."):
                    continue
                if (serial, manifest.parent.name) in excluded:
                    continue
                generation = manifest.parent.name
                fsid = int(digest([serial, generation])[:15], 16) + 1
                lines.append(f"{pseudo / serial / 'roots' / generation} {ip}(ro,fsid={fsid},sync,no_subtree_check,no_root_squash,insecure)")
            fsid = int(digest([serial, 'appdata'])[:15], 16) + 1
            lines.append(f"{pseudo / serial / 'appdata'} {ip}(rw,fsid={fsid},sync,no_subtree_check,no_root_squash,insecure)")
        return "\n".join(lines) + "\n"
