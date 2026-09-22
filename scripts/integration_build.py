"""Opt-in real ARM64 image build in a privileged, disposable Linux container.

See CONTRIBUTING.md for invocation. Downloads the current official Lite image.
Does not start NFS, alter DHCP, or contact any Raspberry Pi.
"""
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pxe_fleet"))

from fleet.build import Builder, chroot
from fleet.config import validate
from fleet.images import discover
from fleet.state import Store

logging.basicConfig(level=logging.INFO)
cfg = validate({"server_ip": "192.0.2.1", "dns": ["192.0.2.1"], "clients": [
    {"serial": "12345678", "ip": "192.0.2.2", "hostname": "test-pi", "model": "pi4"}
]})
store = Store(Path("/data/fleet"))
if set(store.state["clients"]) - {"12345678"}:
    raise RuntimeError("Refusing to use a volume containing other clients")
store.register(cfg)
store.state["clients"]["12345678"]["pending"] = None
store.save()
with Builder(store.path).base(discover()) as (base, fingerprint):
    for flavor in ("v8", "2712"):
        listing = chroot(base, "lsinitramfs", "/boot/firmware/fleet-initrd-" + flavor, output=True)
        assert "scripts/init-bottom/fleet-overlay" in listing
        assert "mount.nfs" in listing
        assert "overlay.ko" in listing
        chroot(base, "unmkinitramfs", "/boot/firmware/fleet-initrd-" + flavor, "/tmp/fleet-initrd-check")
        helpers = list((base / "tmp/fleet-initrd-check").rglob("nfsmount"))
        assert any(p.is_file() and p.read_bytes().startswith(b"#!/bin/sh\nexec /sbin/mount.nfs") for p in helpers)
        import shutil
        shutil.rmtree(base / "tmp/fleet-initrd-check")
    generation = store.stage(cfg, cfg["clients"][0], base, fingerprint)
    store.activate("12345678", generation)
    print("Successfully built and staged real generation", generation, flush=True)
