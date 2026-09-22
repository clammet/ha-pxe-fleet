import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fleet.config import ConfigError, validate, client_spec
from fleet.images import partitions, fetch_verified
from fleet.state import Store
from fleet.util import write_json


def config():
    return validate({"server_ip": "192.0.2.1", "clients": [{"serial": "000000001234abcd", "ip": "192.0.2.2", "hostname": "pi-test", "model": "pi4"}]})


class ConfigTests(unittest.TestCase):
    def test_serial_normalization_and_collision(self):
        cfg = config()
        self.assertEqual(cfg["clients"][0]["serial"], "1234abcd")
        cfg["clients"].append({"serial": "ffffffff1234abcd", "ip": "192.0.2.3", "hostname": "other", "model": "pi5"})
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_reject_unknown_fields_and_unsafe_mounts(self):
        cfg = config()
        cfg["clietns"] = []
        with self.assertRaises(ConfigError):
            validate(cfg)
        for source, target in (("../other", "/var/lib/app"), ("state", "/var/lib/dpkg"), ("/tmp", "/var/lib/app"), ("state", "/etc")):
            cfg = config()
            cfg["clients"][0]["apt"]["persistent"] = [{"source": source, "target": target, "uid": 0, "gid": 0}]
            with self.assertRaises(ConfigError):
                validate(cfg)

    def test_package_arguments_cannot_be_flags(self):
        for bad in ("--allow-unauthenticated", "a; reboot", "linux-image-rpi-v8"):
            cfg = config()
            cfg["clients"][0]["apt"]["packages"] = [bad]
            with self.assertRaises(ConfigError):
                validate(cfg)

    def test_signed_sources_and_stable_service_users(self):
        cfg = config()
        cfg["clients"][0]["apt"] = {"sources": [{"name": "example", "url": "https://example.org/apt", "suites": "{codename}", "key_url": "https://example.org/key.asc", "key_sha256": "a" * 64}], "users": [{"name": "app", "uid": 2100, "gid": 2100}]}
        valid = validate(cfg)
        self.assertEqual(valid["clients"][0]["apt"]["sources"][0]["suites"], "{codename}")
        cfg["clients"][0]["apt"]["sources"][0]["url"] = "http://example.org/apt"
        with self.assertRaises(ConfigError):
            validate(cfg)


class ImageTests(unittest.TestCase):
    def test_partition_offsets(self):
        self.assertEqual(partitions("1 8192 1024\n2 9216 4096\n"), {1: (4194304, 524288), 2: (4718592, 2097152)})
        for bad in ("1 0 123\n2 234 456", "1 123 456", "1 123 456\n1 789 123"):
            with self.assertRaises(ValueError):
                partitions(bad)

    def test_bad_download_never_replaces_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "image.xz"
            file.write_bytes(b"old")
            with patch("fleet.images.open_url", return_value=io.BytesIO(b"corrupt")):
                with self.assertRaises(ValueError):
                    fetch_verified("https://example.org/image", file, hashlib.sha256(b"new").hexdigest())
            self.assertEqual(file.read_bytes(), b"old")
            self.assertFalse(file.with_suffix(".partial").exists())


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.cfg = config()
        self.client = self.cfg["clients"][0]
        self.serial = self.client["serial"]
        self.store.register(self.cfg)
        self.old, self.new = "a" * 24, "b" * 24
        for generation in (self.old, self.new):
            root = self.store.root(self.serial, generation)
            (root / "boot/firmware").mkdir(parents=True)
            (root / "important").write_text(generation)
            write_json(root.parent / "manifest.json", {"client": self.client, "created": 0})
        entry = self.store.state["clients"][self.serial]
        entry["active"] = self.old
        self.store.save()
        self.store.recover()
        self.data = self.store.path / "appdata" / self.serial / "database"
        self.data.write_text("precious")

    def report(self, generation, healthy=False, updating=False):
        return {"generation": generation, "healthy": healthy, "nonce": "test", "updating": updating}

    def test_publish_waits_for_confirmation_and_survives_restart(self):
        self.store.activate(self.serial, self.new)
        self.assertEqual(self.store.state["clients"][self.serial]["active"], self.old)
        reply = self.store.report(self.serial, self.report(self.old, True), 900, now=100)
        self.assertTrue(reply["reboot"])
        restarted = Store(self.store.path)
        restarted.recover()
        self.assertEqual(restarted.desired(self.serial), self.new)
        self.store.report(self.serial, self.report(self.new, True), 900, now=200)
        self.assertEqual(self.store.state["clients"][self.serial]["active"], self.new)
        self.assertEqual(self.store.state["clients"][self.serial]["previous"], self.old)
        self.assertEqual(self.data.read_text(), "precious")
        self.assertEqual((self.store.root(self.serial, self.old) / "important").read_text(), self.old)

    def test_timeout_reverts_boot_and_quarantines_failed_generation(self):
        self.store.activate(self.serial, self.new)
        self.store.report(self.serial, self.report(self.old), 120, now=100)
        self.store.expire(120, now=221)
        self.assertEqual(self.store.desired(self.serial), self.old)
        self.assertIn(self.new, self.store.state["clients"][self.serial]["failed"])
        self.assertIn("payloads/" + self.old, (self.store.path / "tftp" / self.serial / "config.txt").read_text())
        self.assertEqual(self.data.read_text(), "precious")

    def test_offline_client_and_ongoing_apt_update_do_not_expire(self):
        self.store.activate(self.serial, self.new)
        self.store.expire(120, now=999999)
        self.assertEqual(self.store.desired(self.serial), self.new)
        reply = self.store.report(self.serial, self.report(self.old, updating=True), 120, now=100)
        self.assertFalse(reply["reboot"])
        self.store.expire(120, now=999999)
        self.assertEqual(self.store.desired(self.serial), self.new)

    def test_no_known_good_generation_does_not_reboot_loop(self):
        self.store.state["clients"][self.serial]["active"] = None
        self.store.activate(self.serial, self.new)
        self.store.report(self.serial, self.report(self.new), 120, now=0)
        self.store.expire(120, now=121)
        self.assertEqual(self.store.desired(self.serial), self.new)

    def test_gc_preserves_offline_reported_root_and_grace_period(self):
        entry = self.store.state["clients"][self.serial]
        entry["reported"] = self.report(self.new)
        self.assertEqual(self.store.prune_candidates(now=0), [])
        self.assertEqual(self.store.prune_candidates(now=999999), [])
        entry["reported"] = self.report(self.old)
        self.assertEqual(self.store.prune_candidates(now=1000000), [])
        self.assertEqual(self.store.prune_candidates(now=1000000 + 7 * 86400), [(self.serial, self.new)])

    def test_exports_are_per_client_and_only_appdata_is_writable(self):
        exports = self.store.exports()
        self.assertNotIn("*", exports)
        self.assertEqual(sum("(rw," in line for line in exports.splitlines()), 1)
        self.assertIn(str(self.store.path / "nfs" / self.serial / "appdata"), exports)
        self.assertNotIn(self.new, self.store.exports([(self.serial, self.new)]))

    def test_failed_copy_does_not_publish_or_modify_data(self):
        with patch("fleet.state.run", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.store.stage(self.cfg, self.client, Path("unused"), "new-base")
        self.assertEqual(self.store.desired(self.serial), self.old)
        self.assertEqual(self.data.read_text(), "precious")

    def test_application_staging_failure_keeps_current_boot_target(self):
        base = self.store.path / "base"
        base.mkdir()
        def copy_tree(args):
            shutil.copytree(base, args[-1])
        with patch("fleet.state.run", side_effect=copy_tree), patch("fleet.state.prepare_client"), patch("fleet.state.stage_applications", side_effect=RuntimeError("new OS repository unsupported")):
            with self.assertRaises(RuntimeError):
                self.store.stage(self.cfg, self.client, base, "new-major-release")
        self.assertEqual(self.store.desired(self.serial), self.old)
        self.assertEqual(self.data.read_text(), "precious")
        self.assertEqual(list((self.store.path / "generations" / self.serial).glob(".stage-*")), [])


if __name__ == "__main__":
    unittest.main()
