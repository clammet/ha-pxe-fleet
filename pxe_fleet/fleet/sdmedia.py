"""Build deterministic SD firmware slots from an OS generation and pinned U-Boot."""
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import zlib

from .boot import FIELDS, LIMITS
from .config import architecture
from .images import file_hash
from .sd_layout import FORMAT, SLOT_BYTES, Fat
from .util import canonical, digest

BOOTLOADER = Path(__file__).resolve().parent.parent / "bootloader"
LOADERS = Path(__file__).resolve().parent.parent / "bootloaders"
DTBS = {
    "pi1": ("bcm2708-rpi-b.dtb", "bcm2708-rpi-b-plus.dtb", "bcm2708-rpi-b-rev1.dtb"),
    "pi2": ("bcm2709-rpi-2-b.dtb", "bcm2710-rpi-2-b.dtb"),
    "pi3": ("bcm2710-rpi-3-b.dtb",),
    "pi3plus": ("bcm2710-rpi-3-b-plus.dtb",),
    "pi4": ("bcm2711-rpi-4-b.dtb",),
}
EPOCH = 1783382400


def normalized_serial(value):
    if not re.fullmatch(r"(?:0x)?[a-fA-F0-9]{8,16}", value):
        raise ValueError("Serial must contain 8–16 hexadecimal digits")
    return value.lower().removeprefix("0x")[-8:]


def boot_options(options):
    overlays = set()
    for option in options:
        if not re.fullmatch(r"(?:dtparam|dtoverlay|enable_uart|force_turbo|arm_freq|gpu_mem)=[a-zA-Z0-9_,.=-]+", option):
            raise ValueError("Only peripheral/UART/clock boot options are allowed")
        if option.startswith("gpu_mem=") and option != "gpu_mem=32":
            raise ValueError("SD loaders reserve a fixed memory layout and require gpu_mem=32")
        if option.startswith("dtoverlay="):
            name = option.split("=", 1)[1].split(",", 1)[0]
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                raise ValueError("Invalid overlay name")
            overlays.add("overlays/" + name + ".dtbo")
    return sorted(overlays)


def firmware_names(model, options=()):
    suffix = "4" if model == "pi4" else ""
    return ("bootcode.bin", f"start{suffix}.elf", f"fixup{suffix}.dat",
            *DTBS[model], "LICENCE.broadcom", *boot_options(options))


def script_image(text, model):
    script = text.encode()
    payload = struct.pack("!II", len(script), 0) + script
    header = struct.pack("!7I4B32s", 0x27051956, 0, EPOCH, len(payload), 0, 0,
        zlib.crc32(payload), 5, 2 if model in ("pi1", "pi2") else 22, 6, 0, b"PXE Fleet SD loader v2")
    return header[:4] + struct.pack("!I", zlib.crc32(header)) + header[8:] + payload


def boot_script(model, serial, server):
    serial = normalized_serial(serial)
    server = str(ipaddress.IPv4Address(server))
    fleet_model = "pi3" if model == "pi3plus" else model
    arch = architecture(fleet_model)
    limit_kernel, limit_initrd = LIMITS[arch]
    values = {
        "MODEL": fleet_model, "BOOT_MODEL": model, "SERIAL": serial, "SERVER": server,
        "FIELDS": " ".join(FIELDS),
        "KERNEL_ADDR": "0x02000000" if arch == "armhf" else "0x00080000",
        "INITRD_ADDR": "0x06000000" if arch == "armhf" else "0x08000000",
        "FDT_ADDR": "0x0a000000" if arch == "armhf" else "0x18000000",
        "KERNEL_LIMIT": hex(limit_kernel), "INITRD_LIMIT": hex(limit_initrd),
        "BOOT": "bootz" if arch == "armhf" else "booti",
    }
    text = (BOOTLOADER / "boot.cmd").read_text()
    for key, value in values.items():
        text = text.replace("@" + key + "@", value)
    if re.search(r"@[A-Z_]+@", text):
        raise ValueError("Unexpanded boot script placeholder")
    return text


def slot_files(firmware, loader, model, serial, server, options=()):
    serial = normalized_serial(serial)
    files = {name: (firmware / name).read_bytes() for name in firmware_names(model, options)}
    files["u-boot.bin"] = loader.read_bytes()
    if not files["u-boot.bin"]:
        raise ValueError("Empty U-Boot binary")
    files["config.txt"] = (
        f"[all]\nkernel=u-boot.bin\narm_64bit={int(model not in ('pi1', 'pi2'))}\n"
        "auto_initramfs=0\ncmdline=uboot-cmdline.txt\nenable_uart=1\ngpu_mem=32\n"
        + "".join(option + "\n" for option in options)).encode()
    files["uboot-cmdline.txt"] = b"\n"
    files["boot.scr"] = script_image(boot_script(model, serial, server), model)
    metadata = {"format": FORMAT, "model": model, "serial": serial,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    metadata["revision"] = digest(metadata)
    files["slot.id"] = canonical(metadata) + b"\n"
    return files, metadata


def make_fat(files, destination):
    with destination.open("wb") as stream:
        stream.truncate(SLOT_BYTES)
    subprocess.run(["mkfs.fat", "--invariant", "-F", "32", "-n", "FLEET_BOOT", str(destination)],
                   check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        card = Path(tmp)
        for name, data in files.items():
            path = card / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for path in sorted(card.rglob("*"), reverse=True):
            os.utime(path, (EPOCH, EPOCH))
        for path in sorted(card.iterdir()):
            subprocess.run(["mcopy", "-m", "-s", "-i", str(destination), str(path), "::/"], check=True)
    with destination.open("rb") as stream:
        # Refuse malformed/oversized slot metadata while still in private staging.
        Fat(stream).metadata()


def prepare_updates(root, spec):
    if spec["model"] == "pi5" or not spec.get("sd_updates", True):
        return
    models = ("pi3", "pi3plus") if spec["model"] == "pi3" else (spec["model"],)
    directory = root / "usr/lib/pxe-fleet/sd-updates"
    directory.mkdir(parents=True, exist_ok=True)
    descriptions = {}
    for model in models:
        files, metadata = slot_files(root / "boot/firmware", LOADERS / model / "u-boot.bin",
                                     model, spec["serial"], spec["server_ip"], spec["boot_options"])
        image = directory / (model + ".img")
        try:
            make_fat(files, image)
            archive = directory / (model + ".img.gz")
            with image.open("rb") as source, archive.open("wb") as target:
                with gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as compressed:
                    shutil.copyfileobj(source, compressed, 1024 * 1024)
            if archive.stat().st_size > 32 * 1024**2:
                raise ValueError("SD update exceeds the client's download limit")
            descriptions[model] = {"format": FORMAT, "model": model, "serial": spec["serial"],
                "revision": metadata["revision"], "sha256": file_hash(archive), "size": archive.stat().st_size,
                "raw_sha256": file_hash(image), "raw_size": SLOT_BYTES}
        finally:
            image.unlink(missing_ok=True)
    (directory / "index.json").write_bytes(canonical(descriptions) + b"\n")
