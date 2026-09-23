"""Versioned SD-loader protocol. Native firmware boot keeps its own kernel format."""
import gzip
import re

from .config import architecture
from .images import file_hash
from .util import atomic_write

# These bounds match the non-overlapping load regions in boot-media's script.
LIMITS = {"armhf": (32 * 1024**2, 48 * 1024**2), "arm64": (64 * 1024**2, 128 * 1024**2)}
FIELDS = ("fleet_format", "fleet_model", "fleet_serial", "fleet_generation",
          "fleet_kernel", "fleet_kernel_size", "fleet_kernel_sha256",
          "fleet_initrd", "fleet_initrd_size", "fleet_initrd_sha256", "fleet_args")


def sd_payload(boot, client, generation):
    """Prepare and validate everything before the matching generation and immutable boot payload are exported."""
    model = client["model"]
    if model == "pi5":
        return  # No supported U-Boot SD loader for this board yet.
    serial = client["serial"]
    if not re.fullmatch(r"[a-f0-9]{8}", serial) or not re.fullmatch(r"[a-f0-9]{24}", generation):
        raise ValueError("Invalid boot payload identity")
    arch = architecture(model)
    kernel_limit, initrd_limit = LIMITS[arch]
    kernel = boot / "fleet-kernel"
    with kernel.open("rb") as stream:
        compressed = stream.read(2) == b"\x1f\x8b"
    # Pi firmware accepts gzip Image, but booti needs a raw Image. ARM32 bootz
    # takes the self-decompressing zImage as-is. Never change the native payload.
    with (gzip.open(kernel, "rb") if compressed else kernel.open("rb")) as stream:
        data = stream.read(kernel_limit + 1)
    if len(data) > kernel_limit:
        raise ValueError(f"{model} SD kernel exceeds its RAM load region")
    magic = data[56:60] if arch == "arm64" else data[36:40]
    if magic != (b"ARM\x64" if arch == "arm64" else b"\x18\x28\x6f\x01"):
        raise ValueError(f"Unexpected {arch} kernel format for SD boot")
    initrd = boot / "fleet-initrd"
    if not 0 < initrd.stat().st_size <= initrd_limit:
        raise ValueError(f"{model} SD initramfs exceeds its RAM load region")
    atomic_write(boot / "sd-kernel", data)
    prefix = f"{serial}/payloads/{generation}/"
    args = (boot / "cmdline.txt").read_text().strip()
    if not args or any(c in args for c in "\n\r\x00"):
        raise ValueError("Invalid kernel command line")
    values = ("1", model, serial, generation, prefix + "sd-kernel", hex(len(data)),
              file_hash(boot / "sd-kernel"), prefix + "fleet-initrd", hex(initrd.stat().st_size),
              file_hash(initrd), args)
    atomic_write(boot / "sd-boot.env", "".join(f"{key}={value}\n" for key, value in zip(FIELDS, values)))
