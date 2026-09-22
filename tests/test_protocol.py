import hashlib
import hmac
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from fleet.client import exchange, data_directory, apt_install
from fleet.server import handler
from fleet.state import Store
from fleet.util import canonical
from fleet.sdclient import download
from test_fleet import config


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config()
        self.store = Store(self.temp.name)
        self.store.register(self.cfg)
        self.serial = self.cfg["clients"][0]["serial"]
        self.entry = self.store.state["clients"][self.serial]
        self.entry["client"]["ip"] = "127.0.0.1"
        self.generation = "c" * 24
        self.store.root(self.serial, self.generation).mkdir(parents=True)
        self.entry["active"] = self.generation
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.store, self.cfg))
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.spec = {"server_ip": "127.0.0.1", "control_port": self.server.server_port, "serial": self.serial, "token": self.entry["token"]}

    def status(self):
        return {"generation": self.generation, "healthy": True, "boot_id": "11111111-1111-1111-1111-111111111111"}

    def test_authenticated_exchange_and_wrong_key(self):
        reply = exchange(self.spec, self.status())
        self.assertEqual(reply["desired"], self.generation)
        self.assertFalse(reply["reboot"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            exchange({**self.spec, "token": "wrong"}, self.status())
        self.assertEqual(error.exception.code, 403)

    def test_replayed_report_is_rejected(self):
        body = canonical({**self.status(), "nonce": "a" * 48})
        signature = hmac.new(self.entry["token"].encode(), body, hashlib.sha256).hexdigest()
        url = f"http://127.0.0.1:{self.server.server_port}/v1/clients/{self.serial}"
        request = urllib.request.Request(url, data=body, headers={"X-Fleet-Signature": signature})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 400)

    def test_valid_signature_cannot_replay_old_response(self):
        payload = canonical({"nonce": "old", "reboot": True, "desired": "b" * 24})
        response = io.BytesIO(payload)
        response.headers = {"X-Fleet-Signature": hmac.new(self.entry["token"].encode(), payload, hashlib.sha256).hexdigest()}
        with patch("fleet.client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Replayed"):
                exchange(self.spec, self.status())

    def test_unknown_root_cannot_be_confirmed(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            exchange(self.spec, {**self.status(), "generation": "e" * 24})
        self.assertEqual(error.exception.code, 400)

    def test_sd_download_requires_authentication_and_matches_signed_description(self):
        directory = self.store.root(self.serial, self.generation) / "usr/lib/pxe-fleet/sd-updates"
        directory.mkdir(parents=True)
        payload = b"compressed-firmware-placeholder"
        (directory / "pi4.img.gz").write_bytes(payload)
        description = {"format": 2, "model": "pi4", "serial": self.serial, "revision": "d" * 64,
                       "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
                       "raw_size": 64 * 1024**2, "raw_sha256": "e" * 64}
        (directory / "index.json").write_bytes(canonical({"pi4": description}))
        status = {**self.status(), "sd": {"format": 2, "model": "pi4", "serial": self.serial,
                  "card_id": "a" * 32, "revision": "b" * 64, "trial": False, "retry": 0, "failed_generation": None}}
        reply = exchange(self.spec, status)
        self.assertEqual(reply["sd_update"], {**description, "generation": self.generation})
        destination = Path(self.temp.name) / "download"
        download(self.spec, reply["sd_update"], destination)
        self.assertEqual(destination.read_bytes(), payload)
        with self.assertRaises(urllib.error.HTTPError) as error:
            download({**self.spec, "token": "wrong"}, reply["sd_update"], destination)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaisesRegex(ValueError, "checksum"):
            download(self.spec, {**reply["sd_update"], "sha256": "0" * 64}, destination)


class ClientTests(unittest.TestCase):
    def test_initial_data_is_seeded_once_and_copy_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            seed = Path(directory) / "seed"
            data.mkdir(); seed.mkdir()
            (seed / "value").write_text("factory")
            with patch("fleet.client.DATA", data):
                target = data_directory("app", os.getuid(), os.getgid(), seed=seed)
                self.assertEqual((target / "value").read_text(), "factory")
                (target / "value").write_text("user data")
                data_directory("app", os.getuid(), os.getgid(), seed=seed)
                self.assertEqual((target / "value").read_text(), "user data")
                with patch("fleet.client.run", side_effect=RuntimeError("copy failed")):
                    with self.assertRaises(RuntimeError):
                        data_directory("new-app", os.getuid(), os.getgid(), seed=seed)
                self.assertFalse((data / "new-app").exists())
                self.assertEqual(list(data.glob(".fleet-seed-*")), [])

    def test_symlink_cannot_redirect_persistence_outside_appdata(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            (data / "escape").symlink_to(Path(directory))
            with patch("fleet.client.DATA", data):
                with self.assertRaises(RuntimeError):
                    data_directory("escape/dangerous")
            self.assertFalse((Path(directory) / "dangerous").exists())

    def test_apt_failure_does_not_report_success(self):
        cfg = config()["clients"][0]
        cfg["apt"]["packages"] = ["example"]
        with tempfile.TemporaryFile(mode="w") as lock:
            with patch("fleet.client.open", return_value=lock), patch("fleet.client.package_versions", return_value="example=1"), patch("fleet.client.run", side_effect=RuntimeError("repository unavailable")) as runner:
                with self.assertRaises(RuntimeError):
                    apt_install(cfg)
                self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
