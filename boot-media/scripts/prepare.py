#!/usr/bin/env python3
"""Prepare a Fleet v2 SD card with recovery and two updateable firmware slots."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import urllib.request

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / "pxe_fleet"))
from fleet.sd_layout import FORMAT, IMAGE_BYTES, SECTOR, SLOT_BYTES, STARTS, Fat, mbr, selector
from fleet.sdmedia import (DTBS, boot_options, boot_script, firmware_names, make_fat,
                          normalized_serial, script_image, slot_files)

UBOOT_VERSION = "2026.07"
FIRMWARE_REVISION = "12eeaa12865869b07db760f4bbb7507ec6f1976c"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "pxe-fleet-sd-builder"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if not response.url.startswith("https://"):
            raise ValueError("Firmware download redirected away from HTTPS")
        data = response.read()
    if not data:
        raise ValueError(f"Empty firmware download: {url}")
    return data


def firmware(model, revision, overlays=()):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Firmware revision must be a full commit SHA")
    directory = BASE / ".cache/firmware" / revision
    names = (*firmware_names(model), *overlays)
    manifest = directory / "SHA256SUMS.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    for name in names:
        path = directory / name
        if not path.is_file() or previous.get(name) != sha(path):
            data = fetch(f"https://raw.githubusercontent.com/raspberrypi/firmware/{revision}/boot/{name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".partial")
            temporary.write_bytes(data)
            temporary.replace(path)
        previous[name] = sha(path)
    manifest.write_text(json.dumps(previous, indent=2) + "\n")
    return directory, names


def make_image(files, identity, image):
    with tempfile.TemporaryDirectory(dir=image.parent) as tmp:
        work = Path(tmp)
        recovery = {**files, "autoboot.txt": selector(2),
                    "fleet.id": (json.dumps(identity, sort_keys=True) + "\n").encode(),
                    "card.env": f"fleet_card_id={identity['card_id']}\n".encode()}
        make_fat(recovery, work / "recovery.fat")
        make_fat(files, work / "slot.fat")
        with image.open("w+b") as stream:
            stream.write(mbr(identity["card_id"]))
            stream.truncate(IMAGE_BYTES)
            for part in (1, 2, 3):
                stream.seek(STARTS[part] * SECTOR)
                with (work / ("recovery.fat" if part == 1 else "slot.fat")).open("rb") as source:
                    shutil.copyfileobj(source, stream, 1024 * 1024)
        with image.open("rb") as stream:
            fat = Fat(stream, STARTS[1] * SECTOR)
            fat.selector_offset()  # Required for metadata-free, one-sector commits.
            assert fat.metadata("FLEET   ID ") == identity
    image.with_suffix(".img.sha256").write_text(f"{sha(image)}  {image.name}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=DTBS, required=True)
    parser.add_argument("--serial", required=True, help="Pi serial, matching fleet.yaml")
    parser.add_argument("--server", required=True, help="Home Assistant's reserved IPv4 address")
    parser.add_argument("--firmware-revision", default=FIRMWARE_REVISION)
    parser.add_argument("--boot-option", action="append", default=[])
    args = parser.parse_args()
    serial = normalized_serial(args.serial)
    server = str(ipaddress.IPv4Address(args.server))
    overlays = boot_options(args.boot_option)
    binaries = BASE / "out/bin" / args.model
    import subprocess
    subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=binaries, check=True)
    destination = BASE / "out" / f"{serial}-{args.model}"
    if destination.exists():
        raise ValueError(f"{destination.name} exists; move it aside before preparing another image")
    source, _ = firmware(args.model, args.firmware_revision, overlays)
    files, metadata = slot_files(source, binaries / "u-boot.bin", args.model, serial, server, args.boot_option)
    identity = {"format": FORMAT, "card_id": secrets.token_hex(16), "model": args.model, "serial": serial}
    with tempfile.TemporaryDirectory(dir=BASE / "out", prefix="prepare-") as tmp:
        work = Path(tmp)
        card = work / "sd"
        card.mkdir()
        for name, data in files.items():
            path = card / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name in ("COPYING", "UBOOT-LICENSING", "uboot.config"):
            shutil.copy2(binaries / name, work / name)
        (work / "boot.cmd").write_text(boot_script(args.model, serial, server))
        (work / "metadata.json").write_text(json.dumps({**metadata, **identity,
            "server": server, "firmware_revision": args.firmware_revision, "uboot_version": UBOOT_VERSION,
            "uboot_source": f"https://ftp.denx.de/pub/u-boot/u-boot-{UBOOT_VERSION}.tar.bz2",
            "boot_options": args.boot_option}, indent=2) + "\n")
        make_image(files, identity, work / "boot.img")
        work.rename(destination)
    print(f"Prepared out/{destination.name}/boot.img (recovery + A/B slots). Flash with Raspberry Pi Imager (Use custom).")


if __name__ == "__main__":
    main()
