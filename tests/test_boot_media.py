"""Boot protocol, actual disk layout, and corruption handling."""
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from fleet.boot import FIELDS, sd_payload
from fleet.config import architecture, kernel_flavor, validate

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sd_prepare", BASE / "boot-media/scripts/prepare.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def kernel(arch):
    data = bytearray(4096)
    if arch == "arm64":
        data[56:60] = b"ARM\x64"
    else:
        data[36:40] = b"\x18\x28\x6f\x01"
    return bytes(data)


class PayloadTests(unittest.TestCase):
    def test_models_select_matching_kernel_and_architecture(self):
        for model, arch, flavor in (("pi1", "armhf", "v6"), ("pi2", "armhf", "v7"),
                                     ("pi3", "arm64", "v8"), ("pi4", "arm64", "v8"), ("pi5", "arm64", "2712")):
            cfg = validate({"server_ip": "192.0.2.1", "clients": [
                {"serial": "1234abcd", "ip": "192.0.2.2", "hostname": "test", "model": model}]})
            self.assertEqual(architecture(cfg["clients"][0]["model"]), arch)
            self.assertEqual(kernel_flavor(model), flavor)

    def test_manifest_and_sd_kernel_preserve_native_payload(self):
        for model, arch in (("pi1", "armhf"), ("pi2", "armhf"), ("pi3", "arm64"), ("pi4", "arm64")):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                boot = Path(tmp)
                raw = kernel(arch)
                native = gzip.compress(raw) if arch == "arm64" else raw
                (boot / "fleet-kernel").write_bytes(native)
                (boot / "fleet-initrd").write_bytes(b"initramfs")
                (boot / "cmdline.txt").write_text("root=/dev/nfs boot=nfs ip=dhcp\n")
                sd_payload(boot, {"serial": "1234abcd", "model": model}, "a" * 24)
                env = dict(line.split("=", 1) for line in (boot / "sd-boot.env").read_text().splitlines())
                self.assertEqual(set(env), set(FIELDS))
                self.assertEqual((boot / "fleet-kernel").read_bytes(), native)
                self.assertEqual((boot / "sd-kernel").read_bytes(), raw)
                self.assertEqual(env["fleet_kernel_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(int(env["fleet_initrd_size"], 16), 9)
                self.assertEqual(env["fleet_kernel"], "1234abcd/payloads/" + "a" * 24 + "/sd-kernel")

    def test_wrong_architecture_and_oversize_fail_before_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "fleet-kernel").write_bytes(kernel("arm64"))
            (boot / "fleet-initrd").write_bytes(b"initramfs")
            (boot / "cmdline.txt").write_text("boot=nfs\n")
            with self.assertRaises(ValueError):
                sd_payload(boot, {"serial": "1234abcd", "model": "pi2"}, "a" * 24)
            with patch("fleet.boot.LIMITS", {"arm64": (100, 100)}), self.assertRaises(ValueError):
                sd_payload(boot, {"serial": "1234abcd", "model": "pi4"}, "a" * 24)
            self.assertFalse((boot / "sd-boot.env").exists())


class MediaTests(unittest.TestCase):
    def test_network_boot_retries_entire_operation_until_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # The distro function encompasses DHCP plus mounting. Fail twice,
            # then succeed; exercise the production loop with no actual sleeps.
            (work / "nfs").write_text('''nfs_top() { :; }
modprobe() { :; }
wait_for_udev() { :; }
sleep() { :; }
attempts=0
nfs_mount_root_impl() {
    attempts=$((attempts + 1))
    [ "$attempts" -ge 3 ]
}
''')
            script = (BASE / "pxe_fleet/assets/fleet-nfs").read_text().replace(". /scripts/nfs", '. "$FLEET_TEST_NFS"')
            script += '\nmountroot\nprintf "%s" "$attempts"\n'
            result = subprocess.run(["sh"], input=script, text=True, capture_output=True,
                                    env={**os.environ, "FLEET_TEST_NFS": str(work / "nfs")}, timeout=5, check=True)
            self.assertEqual(result.stdout, "3")
            self.assertEqual(result.stderr.count("retrying"), 2)

    def test_mbr_partitions_are_disjoint_and_fit_disk(self):
        from fleet.sd_layout import IMAGE_BYTES, SLOT_BYTES, STARTS
        mbr = media.mbr("a" * 32)
        self.assertEqual(len(mbr), 512)
        self.assertEqual(mbr[510:], b"\x55\xaa")
        previous_end = 1
        for part in (1, 2, 3):
            offset = 446 + (part - 1) * 16
            active, _, kind, _, start, size = struct.unpack("<B3sB3sII", mbr[offset:offset+16])
            self.assertEqual((active, kind, start), (128 if part == 1 else 0, 12, STARTS[part]))
            self.assertGreaterEqual(start, previous_end)
            self.assertEqual(size * 512, SLOT_BYTES)
            previous_end = start + size
        self.assertLessEqual(previous_end * 512, IMAGE_BYTES)
        self.assertEqual(mbr[494:510], bytes(16))

    def test_uboot_image_crc_architecture_and_script_table(self):
        for model in media.DTBS:
            text = media.boot_script(model, "000000001234abcd", "192.0.2.1")
            image = media.script_image(text, model)
            header = struct.unpack("!7I4B32s", image[:64])
            self.assertEqual(header[0], 0x27051956)
            self.assertEqual(header[1], zlib.crc32(image[:4] + bytes(4) + image[8:64]))
            self.assertEqual(header[3], len(image) - 64)
            self.assertEqual(header[6], zlib.crc32(image[64:]))
            self.assertEqual(header[8], 2 if model in ("pi1", "pi2") else 22)
            self.assertEqual(struct.unpack("!II", image[64:72]), (len(text.encode()), 0))
            self.assertEqual(image[72:].decode(), text)

    def test_identity_and_overlay_paths_reject_unsafe_input(self):
        self.assertEqual(media.normalized_serial("000000001234ABCD"), "1234abcd")
        for invalid in ("abc", "1234abcd;reset", "1234abcd\n"):
            with self.assertRaises(ValueError):
                media.normalized_serial(invalid)
        for invalid in ("kernel=bad", "dtoverlay=../../x", "gpu_mem=256", "dtoverlay=foo\nbar"):
            with self.assertRaises(ValueError):
                media.boot_options([invalid])
        self.assertEqual(media.boot_options(["dtoverlay=i2c-rtc,ds3231", "dtparam=i2c_arm=on"]), ["overlays/i2c-rtc.dtbo"])

    def test_corrupt_cached_firmware_is_refetched(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(media, "BASE", Path(tmp)), patch.object(media, "fetch", return_value=b"verified-over-https") as fetch:
            directory, names = media.firmware("pi3", "a" * 40)
            first_count = fetch.call_count
            media.firmware("pi3", "a" * 40)
            self.assertEqual(fetch.call_count, first_count)
            (directory / names[0]).write_bytes(b"corrupt")
            media.firmware("pi3", "a" * 40)
            self.assertEqual(fetch.call_count, first_count + 1)
            self.assertEqual((directory / names[0]).read_bytes(), b"verified-over-https")

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("mkfs.fat", "mcopy", "mkimage")), "Linux SD image tools not installed")
    def test_real_fat_image_extracts_identical_boot_script(self):
        from fleet.sd_layout import Fat, SECTOR, STARTS
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            script = media.script_image(media.boot_script("pi4", "1234abcd", "192.0.2.1"), "pi4")
            identity = {"format": 2, "card_id": "a" * 32, "serial": "1234abcd", "model": "pi4"}
            files = {"boot.scr": script, "slot.id": b'{"format":2}'}
            media.make_image(files, identity, work / "boot.img")
            for part in (1, 2, 3):
                with (work / "boot.img").open("rb") as stream:
                    self.assertEqual(Fat(stream, STARTS[part] * SECTOR).file("BOOT    SCR"), script)
            subprocess.run(["mcopy", "-i", str(work / "boot.img") + "@@1048576", "::/boot.scr", str(work / "extracted.scr")], check=True)
            self.assertEqual((work / "extracted.scr").read_bytes(), script)
            subprocess.run(["mkimage", "-l", str(work / "extracted.scr")], check=True, stdout=subprocess.PIPE)
            self.assertEqual((work / "boot.img.sha256").read_text().split()[0], media.sha(work / "boot.img"))


if __name__ == "__main__":
    unittest.main()
