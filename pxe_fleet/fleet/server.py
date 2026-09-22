"""HA add-on entry point and signed client control endpoint."""
import argparse
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

import yaml

from .build import Builder
from .config import architecture, validate
from .images import discover
from .state import Store
from .util import atomic_write, canonical, run, write_json

LOG = logging.getLogger(__name__)


def handler(store, cfg):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            match = re.fullmatch(r"/v1/clients/([a-f0-9]{8})/sd/([a-f0-9]{24})/(pi1|pi2|pi3|pi3plus|pi4)", self.path)
            if not match or match[1] not in store.state["clients"]:
                self.send_error(404)
                return
            serial, generation, model = match.groups()
            entry = store.state["clients"][serial]
            signature = hmac.new(entry["token"].encode(), ("GET " + self.path).encode(), hashlib.sha256).hexdigest()
            if self.client_address[0] != entry["client"]["ip"] or not hmac.compare_digest(signature, self.headers.get("X-Fleet-Signature", "")):
                self.send_error(403)
                return
            path = store.root(serial, generation) / "usr/lib/pxe-fleet/sd-updates" / (model + ".img.gz")
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                self.send_error(404)
                return
            with stream:
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile, 1024 * 1024)

        def do_POST(self):
            match = re.fullmatch(r"/v1/clients/([a-f0-9]{8})", self.path)
            if not match or match[1] not in store.state["clients"]:
                self.send_error(404)
                return
            serial = match[1]
            entry = store.state["clients"][serial]
            if self.client_address[0] != entry["client"]["ip"]:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError("Invalid request length")
                body = self.rfile.read(size)
                token = entry["token"].encode()
                signature = hmac.new(token, body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, self.headers.get("X-Fleet-Signature", "")):
                    self.send_error(403)
                    return
                status = json.loads(body)
                if (not isinstance(status, dict) or type(status.get("healthy")) is not bool
                    or not re.fullmatch(r"[a-f0-9]{24}", str(status.get("generation", "")))
                    or not re.fullmatch(r"[a-f0-9]{48}", str(status.get("nonce", "")))
                    or not re.fullmatch(r"[a-f0-9-]{36}", str(status.get("boot_id", "")))):
                    raise ValueError("Invalid report")
                if not store.root(serial, status["generation"]).exists():
                    raise ValueError("Unknown generation")
                sd = status.get("sd")
                if sd is not None:
                    if (not isinstance(sd, dict) or sd.get("format") != 2
                        or sd.get("model") not in ("pi1", "pi2", "pi3", "pi3plus", "pi4")
                        or ("pi3" if sd["model"] == "pi3plus" else sd["model"]) != entry["client"]["model"]
                        or sd.get("serial") != serial
                        or not re.fullmatch(r"[a-f0-9]{32}", str(sd.get("card_id", "")))
                        or not re.fullmatch(r"[a-f0-9]{64}", str(sd.get("revision", "")))
                        or type(sd.get("trial")) is not bool
                        or type(sd.get("retry")) is not int or sd["retry"] < 0
                        or sd.get("failed_generation") is not None and not re.fullmatch(r"[a-f0-9]{24}", str(sd["failed_generation"]))):
                        raise ValueError("Invalid SD boot report")
                # Ignore replayed reports. Nonces are not persisted because they
                # cannot authorize an arbitrary boot target, only report health.
                with store.lock:
                    seen = getattr(self.server, "seen", {})
                    key = (serial, status["nonce"])
                    if key in seen:
                        raise ValueError("Replayed report")
                    seen[key] = time.monotonic()
                    self.server.seen = {k: v for k, v in seen.items() if time.monotonic() - v < 3600}
                reply = canonical(store.report(serial, status, cfg["boot_timeout_seconds"]))
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.send_header("X-Fleet-Signature", hmac.new(token, reply, hashlib.sha256).hexdigest())
            self.end_headers()
            self.wfile.write(reply)

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, fmt, *args):
            LOG.debug(fmt, *args)
    return Handler


class Services:
    def __init__(self, store, cfg):
        self.store, self.cfg, self.processes = store, cfg, []

    def exports(self, excluded=()):
        pseudo = self.store.path / "nfs"
        pseudo.mkdir(exist_ok=True)
        for serial in self.store.state["clients"]:
            mappings = [(self.store.path / "appdata" / serial, pseudo / serial / "appdata")]
            for manifest in (self.store.path / "generations" / serial).glob("*/manifest.json"):
                generation = manifest.parent.name
                if generation.startswith(".") or (serial, generation) in excluded:
                    continue
                mappings.append((manifest.parent / "root", pseudo / serial / "roots" / generation))
            for source, target in mappings:
                target.mkdir(parents=True, exist_ok=True)
                if subprocess.run(["mountpoint", "-q", str(target)]).returncode:
                    run(["mount", "--bind", source, target])
        atomic_write(Path("/etc/exports"), self.store.exports(excluded))
        run(["exportfs", "-ra"])

    def unmount_generation(self, serial, generation):
        target = self.store.path / "nfs" / serial / "roots" / generation
        if target.exists() and subprocess.run(["mountpoint", "-q", str(target)]).returncode == 0:
            run(["umount", target])
        if target.exists():
            target.rmdir()

    def spawn(self, args):
        self.processes.append(subprocess.Popen([str(a) for a in args]))

    def start(self):
        path = Path("/proc/fs/nfsd")
        path.mkdir(exist_ok=True)
        if subprocess.run(["mountpoint", "-q", str(path)]).returncode:
            run(["mount", "-t", "nfsd", "nfsd", path])
        threads = int((path / "threads").read_text().strip())
        ownership = self.store.path / "nfs-owner.json"
        identity = {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "server_ip": self.cfg["server_ip"]}
        if threads and (not ownership.exists() or json.loads(ownership.read_text()) != identity):
            raise RuntimeError("Another NFS server is running; refusing to change its exports")
        # NFSv4 uses TCP 2049, with integrated locking. rpc.mountd serves the
        # kernel export cache; it exposes no legacy v2/v3 MOUNT listener.
        recovery = self.store.path / "nfs-recovery"
        recovery.mkdir(exist_ok=True)
        atomic_write(Path("/etc/nfs.conf"), "[nfsd]\nvers3=n\nvers4=y\nvers4.1=y\nvers4.2=y\nudp=n\n"
                     f"[nfsdcltrack]\nstoragedir={recovery}\n")
        self.spawn(["rpc.mountd", "--foreground", "--no-udp", "--no-tcp", "--no-nfs-version", "2", "--no-nfs-version", "3"])
        self.exports()
        if not threads:
            run(["rpc.nfsd", "--host", self.cfg["server_ip"], "--port", "2049", "--no-udp", "--no-nfs-version", "3", "8"])
            write_json(ownership, identity)
        tftp = self.store.path / "tftp"
        tftp.mkdir(exist_ok=True)
        self.spawn(["dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", "--port=0", "--enable-tftp", "--listen-address=" + self.cfg["server_ip"], "--bind-interfaces", "--tftp-root=" + str(tftp), "--log-facility=-", "--user=root"])

    def check(self):
        for process in self.processes:
            if process.poll() is not None:
                raise RuntimeError(f"Network service exited: {process.args}")

    def close(self):
        # Do not unexport running roots on a routine controller restart. Kernel
        # NFS can continue to serve them while this add-on starts back up.
        for process in reversed(self.processes):
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def reconcile(store, cfg, services, builder):
    # A stale/offline client's last reported root stays protected. Only roots
    # unreferenced for a full week can be unexported and removed.
    with store.lock:
        candidates = store.prune_candidates()
        if candidates:
            services.exports(candidates)
            for serial, generation in candidates:
                services.unmount_generation(serial, generation)
                shutil.rmtree(store.root(serial, generation).parent)
                shutil.rmtree(store.path / "tftp" / serial / "payloads" / generation, ignore_errors=True)
                store.state["clients"][serial]["retired"].pop(generation, None)
            store.save()
    for arch in sorted({architecture(c["model"]) for c in cfg["clients"]}):
        try:
            release = discover(cfg["image" if arch == "arm64" else "image_armhf"], arch)
            if shutil.disk_usage(store.path).free < cfg["min_free_gib"] * 1024 ** 3:
                raise RuntimeError("Insufficient free space to stage an OS; current generations retained")
            with builder.base(release, arch) as (base, fingerprint):
                for client in cfg["clients"]:
                    if architecture(client["model"]) != arch:
                        continue
                    try:
                        if shutil.disk_usage(store.path).free < cfg["min_free_gib"] * 1024 ** 3:
                            raise RuntimeError("Insufficient free space for another client generation")
                        generation = store.stage(cfg, client, base, fingerprint)
                        if generation:
                            # Export before publishing a boot target or requesting reboot.
                            services.exports()
                            store.activate(client["serial"], generation)
                    except Exception:
                        LOG.exception("Failed to stage client %s; existing generation retained", client["serial"])
        except Exception:
            LOG.exception("Failed to build %s OS; existing generations retained", arch)


def requests(store):
    check = False
    for path in sorted((store.path / "requests").glob("*.json")):
        try:
            request = json.loads(path.read_text())
            with store.lock:
                command = request["command"]
                if command == "check":
                    check = True
                elif command == "retry":
                    entry = store.state["clients"][request["serial"]]
                    entry["failed"] = []
                    entry["sd_retry"] = entry.get("sd_retry", 0) + 1
                    store.save()
                    check = True
                elif command == "rollback":
                    serial = request["serial"]
                    entry = store.state["clients"][serial]
                    if entry["pending"] or not entry["previous"]:
                        raise ValueError("Rollback requires a confirmed previous generation and no pending rollout")
                    entry["failed"].append(entry["active"])
                    store.activate(serial, entry["previous"])
                else:
                    raise ValueError("Unknown control command")
            LOG.info("Completed local request %s", request)
        except Exception:
            LOG.exception("Local request failed: %s", path.name)
        finally:
            path.unlink(missing_ok=True)
    return check


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--storage", type=Path, default=Path("/data/fleet"))
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    config_path = args.config
    if not config_path:
        name = json.loads(Path("/data/options.json").read_text())["config_file"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.yaml", name):
            raise ValueError("Invalid configuration filename")
        config_path = Path("/config") / name
    cfg = validate(yaml.safe_load(config_path.read_text()))
    if args.validate:
        print("Configuration is valid")
        return
    if not args.storage.is_absolute() or re.search(r"[^a-zA-Z0-9_./-]", str(args.storage)):
        raise ValueError("Storage must be an absolute path without whitespace or special characters")
    store = Store(args.storage)
    lock = (store.path / "controller.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    Builder(store.path).clean_stale()
    store.register(cfg)
    store.recover()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    services = Services(store, cfg)
    server = ThreadingHTTPServer((cfg["server_ip"], cfg["control_port"]), handler(store, cfg))
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    build_thread = None
    try:
        services.start()
        next_check = 0
        while not stop.wait(2):
            services.check()
            store.expire(cfg["boot_timeout_seconds"])
            if requests(store):
                next_check = 0
            if time.monotonic() >= next_check and (build_thread is None or not build_thread.is_alive()):
                def build():
                    try:
                        reconcile(store, cfg, services, Builder(store.path))
                    except Exception:
                        LOG.exception("OS update failed; existing roots remain available")
                build_thread = threading.Thread(target=build, daemon=True)
                build_thread.start()
                next_check = time.monotonic() + cfg["os_check_hours"] * 3600
    finally:
        server.shutdown()
        server.server_close()
        services.close()


if __name__ == "__main__":
    main()
