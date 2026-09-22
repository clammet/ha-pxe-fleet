"""Opt-in real Raspberry Pi OS image build in a privileged, disposable Linux container.

See CONTRIBUTING.md for invocation. Downloads the current official Lite image.
Does not start NFS, alter DHCP, or contact any Raspberry Pi.
"""
import argparse
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pxe_fleet"))

from fleet.build import Builder, chroot
from fleet.config import validate
from fleet.images import discover
from fleet.state import Store
from fleet import sdmedia

# Source is bind-mounted read-only; use the loaders compiled into the test image.
if not sdmedia.LOADERS.exists():
    sdmedia.LOADERS = Path("/opt/pxe-fleet/bootloaders")

logging.basicConfig(level=logging.INFO)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--arch", choices=("arm64", "armhf"), default="arm64")
args = parser.parse_args()
cfg = validate({"server_ip": "192.0.2.1", "dns": ["192.0.2.1"], "clients": [
    {"serial": "12345678", "ip": "192.0.2.2", "hostname": "test-pi", "model": "pi4" if args.arch == "arm64" else "pi2"}
]})
store = Store(Path("/data/fleet"))
if set(store.state["clients"]) - {"12345678"}:
    raise RuntimeError("Refusing to use a volume containing other clients")
store.register(cfg)
store.state["clients"]["12345678"]["pending"] = None
store.save()
with Builder(store.path).base(discover(arch=args.arch), args.arch) as (base, fingerprint):
    for flavor in (("v8", "2712") if args.arch == "arm64" else ("v6", "v7")):
        listing = chroot(base, "lsinitramfs", "/boot/firmware/fleet-initrd-" + flavor, output=True)
        assert "scripts/init-bottom/fleet-overlay" in listing
        assert "scripts/fleet" in listing and "scripts/nfs" in listing
        assert "mount.nfs" in listing
        assert "overlay.ko" in listing
        chroot(base, "unmkinitramfs", "/boot/firmware/fleet-initrd-" + flavor, "/tmp/fleet-initrd-check")
        helpers = list((base / "tmp/fleet-initrd-check").rglob("nfsmount"))
        assert any(p.is_file() and b"exec /sbin/mount.nfs" in p.read_bytes() for p in helpers)
        import shutil
        shutil.rmtree(base / "tmp/fleet-initrd-check")
    generation = store.stage(cfg, cfg["clients"][0], base, fingerprint)
    store.activate("12345678", generation)
    assert "fleet_generation=" + generation in (store.path / "tftp/12345678/boot.env").read_text()
    import json
    index = store.root("12345678", generation) / "usr/lib/pxe-fleet/sd-updates/index.json"
    updates = json.loads(index.read_text())
    model = cfg["clients"][0]["model"]
    assert updates[model]["raw_size"] == 64 * 1024**2
    assert (index.parent / (model + ".img.gz")).is_file()
    print("Successfully built and staged real generation", generation, flush=True)
