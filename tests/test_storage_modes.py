"""Storage selection, creation-time immutability and mount safety."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleet.build import prepare_client
from fleet.client import require_appdata, require_container_storage
from fleet.config import ConfigError, client_spec, validate
from fleet.state import Store
from fleet.storage import container_disk
from fleet.util import write_json
from test_fleet import config


class StorageModeTests(unittest.TestCase):
    def test_mode_defaults_and_mount_sizes_are_validated(self):
        cfg = config()
        self.assertEqual(cfg["clients"][0]["os_storage"], "nfs")
        for mode in (False, "overlay", [], None):
            cfg["clients"][0]["os_storage"] = mode
            with self.assertRaises(ConfigError):
                validate(cfg)
        cfg["clients"][0]["os_storage"] = "ram"
        for field in ("ram_root_size", "ram_data_size", "ram_container_size"):
            for value in ("0%", "100%", "0G", "1G,exec", 123, "1M\n"):
                broken = copy.deepcopy(cfg)
                broken["clients"][0][field] = value
                with self.assertRaises(ConfigError):
                    validate(broken)
            cfg["clients"][0][field] = "512M"
        self.assertEqual(validate(cfg)["clients"][0]["ram_root_size"], "512M")

    def test_mode_is_frozen_on_registration_including_across_restart(self):
        for initial, attempted in (("nfs", "ram"), ("ram", "nfs")):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as tmp:
                cfg = config()
                cfg["clients"][0]["os_storage"] = initial
                Store(tmp).register(cfg)
                cfg["clients"][0]["os_storage"] = attempted
                store = Store(tmp)
                with self.assertLogs("fleet.state", "WARNING"):
                    store.register(cfg)
                self.assertEqual(cfg["clients"][0]["os_storage"], initial)
                self.assertEqual(Store(tmp).state["clients"]["1234abcd"]["os_storage"], initial)

    def test_preexisting_client_without_mode_remains_nfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            store = Store(tmp)
            store.register(cfg)
            entry = store.state["clients"]["1234abcd"]
            del entry["os_storage"]
            del entry["client"]["os_storage"]
            store.save()
            cfg["clients"][0]["os_storage"] = "ram"
            with self.assertLogs("fleet.state", "WARNING"):
                Store(tmp).register(cfg)
            self.assertEqual(cfg["clients"][0]["os_storage"], "nfs")

    def test_ram_mounts_have_no_nfs_or_loop_backing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            client = cfg["clients"][0]
            client.update(os_storage="ram", containers=[{"name": "test", "image": "example.org/app:latest", "environment": {}}])
            prepare_client(Path(tmp), client_spec(cfg, client), "a" * 24, "token", "/1234abcd/appdata")
            lines = (Path(tmp) / "etc/fstab").read_text().splitlines()
            self.assertEqual([(v.split()[1], v.split()[2]) for v in lines], [("/appdata", "tmpfs"), ("/var/lib/containers", "tmpfs")])
            self.assertTrue(all(v.split()[0] == "tmpfs" for v in lines))

    def test_ram_clients_never_provision_or_modify_nfs_container_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            client = {"os_storage": "ram", "containers": [{}]}
            container_disk(path, client, "token")
            self.assertEqual(list(path.iterdir()), [])
            (path / ".fleet").mkdir()
            disk = path / ".fleet/podman.ext4"
            disk.write_bytes(b"retained")
            container_disk(path, client, "token")
            self.assertEqual(disk.read_bytes(), b"retained")

    def test_exports_use_each_generation_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config()
            store = Store(tmp)
            store.register(cfg)
            for generation, mode in (("a" * 24, "ram"), ("b" * 24, "nfs")):
                root = store.root("1234abcd", generation)
                root.mkdir(parents=True)
                write_json(root.parent / "manifest.json", {"client": {**cfg["clients"][0], "os_storage": mode}})
            lines = store.exports().splitlines()
            self.assertIn("(ro,", next(line for line in lines if "a" * 24 in line))
            self.assertIn("(rw,", next(line for line in lines if "b" * 24 in line))

    def test_mount_guards_refuse_a_different_storage_mode(self):
        for mode, expected in (("nfs", "nfs4"), ("ram", "tmpfs")):
            spec = {"os_storage": mode, "containers": [{}]}
            for actual in ("tmpfs", "nfs4", "ext4"):
                mount = json.dumps({"filesystems": [{"fstype": actual, "source": "tmpfs"}]})
                with patch("fleet.client.output", return_value=mount):
                    if actual == expected:
                        require_appdata(spec)
                    else:
                        with self.assertRaises(RuntimeError):
                            require_appdata(spec)
                    if mode == "ram" and actual == "tmpfs":
                        require_container_storage(spec)
                    else:
                        with self.assertRaises(RuntimeError):
                            require_container_storage(spec)
