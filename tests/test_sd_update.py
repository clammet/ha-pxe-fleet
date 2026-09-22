"""Real FAT images plus interrupted-write and update-state-machine tests (no disks)."""
from contextlib import contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fleet import sdclient, sdmedia
from fleet.sd_layout import Fat, SECTOR, SLOT_BYTES, STARTS, selector, validate_mbr
from fleet.util import canonical
from test_boot_media import media


@unittest.skipUnless(all(shutil.which(tool) for tool in ("mkfs.fat", "mcopy")), "FAT tools not installed")
class SDUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.work = Path(cls.tmp.name)
        cls.identity = {"format": 2, "card_id": "a" * 32, "model": "pi4", "serial": "1234abcd", "partition": 2, "tryboot": False}
        firmware = cls.work / "firmware"
        firmware.mkdir()
        for name in sdmedia.firmware_names("pi4"):
            (firmware / name).write_bytes(("original " + name).encode())
        loader = cls.work / "u-boot.bin"
        loader.write_bytes(b"test loader")
        cls.files, cls.old = sdmedia.slot_files(firmware, loader, "pi4", "1234abcd", "192.0.2.1")
        media.make_image(cls.files, {k: cls.identity[k] for k in ("format", "card_id", "model", "serial")}, cls.work / "original.img")
        (firmware / "start4.elf").write_bytes(b"new firmware")
        updated, cls.new = sdmedia.slot_files(firmware, loader, "pi4", "1234abcd", "192.0.2.1")
        sdmedia.make_fat(updated, cls.work / "update.fat")
        cls.archive = cls.work / "update.img.gz"
        with (cls.work / "update.fat").open("rb") as source, gzip.open(cls.archive, "wb") as target:
            shutil.copyfileobj(source, target)
        cls.description = {"format": 2, "model": "pi4", "serial": "1234abcd", "revision": cls.new["revision"],
            "size": cls.archive.stat().st_size, "sha256": media.sha(cls.archive), "raw_size": SLOT_BYTES,
            "raw_sha256": media.sha(cls.work / "update.fat"), "generation": "b" * 24}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.image = self.path / "card.img"
        shutil.copyfile(self.work / "original.img", self.image)
        self.state = self.path / "state.json"
        self.spec = {"serial": "1234abcd", "model": "pi4", "sd_updates": True, "generation": "b" * 24}
        self.reply = {"desired": "b" * 24, "reboot": True, "sd_update": self.description, "sd_retry": 0}

    @contextmanager
    def card(self, device, identity, writable=False):
        with self.image.open("r+b" if writable else "rb") as stream:
            yield sdclient.Card(stream, identity)

    def manager(self, identity=None, boot_id="old-boot"):
        return sdclient.Manager(self.spec, identity or self.identity, self.image, self.state, boot_id)

    def part_hash(self, part):
        result = hashlib.sha256()
        with self.image.open("rb") as stream:
            stream.seek(STARTS[part] * SECTOR)
            for _ in range(SLOT_BYTES // (1024 * 1024)):
                result.update(stream.read(1024 * 1024))
        return result.hexdigest()

    def fake_download(self, spec, description, destination):
        shutil.copyfile(self.archive, destination)

    def test_install_only_changes_inactive_slot_and_commit_only_selector(self):
        recovery, active = self.part_hash(1), self.part_hash(2)
        with self.card(None, self.identity, True) as card:
            card.install(self.archive, self.description, 3)
            self.assertEqual(card.active(), 2)
            self.assertEqual(card.revision(3), self.new["revision"])
        self.assertEqual(self.part_hash(1), recovery)
        self.assertEqual(self.part_hash(2), active)
        before = self.image.read_bytes()
        trial = {**self.identity, "partition": 3, "tryboot": True}
        with self.card(None, trial, True) as card:
            offset = card.selector_offset
            self.assertTrue(card.commit(3, self.new["revision"]))
            self.assertFalse(card.commit(3, self.new["revision"]))
        after = self.image.read_bytes()
        self.assertEqual(before[:offset], after[:offset])
        self.assertEqual(before[offset+SECTOR:], after[offset+SECTOR:])
        self.assertEqual(after[offset:offset+SECTOR], selector(3))

    def test_reject_active_slot_wrong_identity_and_corrupt_payload(self):
        with self.card(None, self.identity, True) as card:
            for part in (1, 2):
                with self.assertRaises(ValueError):
                    card.install(self.archive, self.description, part)
            with patch("fleet.sdclient.write_at") as write:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    card.install(self.archive, {**self.description, "raw_sha256": "f" * 64}, 3)
                write.assert_not_called()
        with self.assertRaises(ValueError), self.card(None, {**self.identity, "serial": "deadbeef"}):
            pass
        with self.image.open("rb") as stream, self.assertRaises(ValueError):
            validate_mbr(stream, "b" * 32)

    def test_interrupted_inactive_write_preserves_recovery_and_active(self):
        old = (self.part_hash(1), self.part_hash(2))
        original = sdclient.write_at
        count = 0
        def interrupted(stream, offset, data):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("power loss")
            return original(stream, offset, data)
        with self.card(None, self.identity, True) as card, patch("fleet.sdclient.write_at", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "power loss"):
                card.install(self.archive, self.description, 3)
        self.assertEqual((self.part_hash(1), self.part_hash(2)), old)
        with self.card(None, self.identity) as card:
            self.assertEqual(card.active(), 2)

    def test_trial_commits_only_after_health_and_unchanged_revision_never_writes(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download) as download:
            manager = self.manager()
            self.assertEqual(manager.handle(self.reply, True), "tryboot")
            self.assertEqual(manager.handle(self.reply, True), "tryboot")
            self.assertEqual(download.call_count, 1)
            trial = self.manager({**self.identity, "partition": 3, "tryboot": True}, "new-boot")
            self.assertTrue(trial.trial)
            self.assertEqual(trial.handle(self.reply, False), "wait")
            with self.card(None, self.identity) as card:
                self.assertEqual(card.active(), 2)
            self.assertEqual(trial.handle(self.reply, True), "ready")
            self.assertFalse(trial.trial)
            with self.card(None, self.identity) as card:
                self.assertEqual(card.active(), 3)
            with patch("fleet.sdclient.write_at") as write:
                for _ in range(3):
                    self.assertEqual(trial.handle(self.reply, True), "ready")
                write.assert_not_called()
            self.assertEqual(download.call_count, 1)

    def test_failed_trial_is_quarantined_and_explicit_retry_releases_it(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download) as download:
            self.manager().handle(self.reply, True)
            fallback = self.manager(boot_id="fallback-boot")
            self.assertEqual(fallback.report()["failed_generation"], "b" * 24)
            self.assertEqual(fallback.handle(self.reply, True), "wait")
            self.assertEqual(download.call_count, 1)
            self.assertEqual(fallback.handle({**self.reply, "sd_retry": 1}, True), "tryboot")
            self.assertEqual(download.call_count, 2)

    def test_restart_after_selector_commit_recovers_without_another_sd_write(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download):
            self.manager().handle(self.reply, True)
            trial_id = {**self.identity, "partition": 3, "tryboot": True}
            with self.card(None, trial_id, True) as card:
                card.commit(3, self.new["revision"])
            with patch("fleet.sdclient.write_at") as write:
                rebooted = self.manager({**trial_id, "tryboot": False}, "after-commit")
                self.assertEqual(rebooted.handle(self.reply, True), "ready")
                self.assertIsNone(rebooted.state["pending"])
                write.assert_not_called()

    def test_controller_rollback_during_trial_keeps_original_selector(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download):
            self.manager().handle(self.reply, True)
            trial = self.manager({**self.identity, "partition": 3, "tryboot": True}, "new-boot")
            self.assertEqual(trial.handle({**self.reply, "desired": "a" * 24}, True), "reboot")
            with self.card(None, self.identity) as card:
                self.assertEqual(card.active(), 2)

    def test_firmware_only_trial_times_out_without_an_os_rollout(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download):
            self.manager().handle(self.reply, True)
            trial = self.manager({**self.identity, "partition": 3, "tryboot": True}, "new-boot")
            started = trial.state["pending"]["trial_started"]
            # Restarting the agent on the same boot must not reset the deadline.
            trial = self.manager({**self.identity, "partition": 3, "tryboot": True}, "new-boot")
            self.assertEqual(trial.state["pending"]["trial_started"], started)
            with patch("fleet.sdclient.time.monotonic", return_value=started + 901):
                self.assertEqual(trial.handle({**self.reply, "reboot": False}, False), "reboot")
            with self.card(None, self.identity) as card:
                self.assertEqual(card.active(), 2)

    def test_trial_deadline_does_not_need_a_controller_reply(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download):
            self.manager().handle(self.reply, True)
            trial = self.manager({**self.identity, "partition": 3, "tryboot": True}, "new-boot")
            with patch("fleet.sdclient.time.monotonic", return_value=trial.state["pending"]["trial_started"] + 901):
                self.assertTrue(trial.expire_trial())
            self.assertEqual(trial.report()["failed_generation"], self.reply["desired"])
            with self.card(None, self.identity) as card:
                self.assertEqual(card.active(), 2)

    def test_healthy_wrong_os_cannot_commit_a_trial(self):
        with patch("fleet.sdclient.open_card", self.card), patch("fleet.sdclient.RUNTIME", self.path), patch("fleet.sdclient.download", side_effect=self.fake_download):
            self.manager().handle(self.reply, True)
            self.spec["generation"] = "a" * 24
            trial = self.manager({**self.identity, "partition": 3, "tryboot": True}, "new-boot")
            self.assertEqual(trial.handle(self.reply, True), "reboot")
            with self.card(None, self.identity) as card:
                self.assertEqual(card.active(), 2)

    def test_unchanged_boot_files_have_same_revision(self):
        # Revision depends on boot contents, not OS generation, mtime, or time.
        metadata = {k: self.old[k] for k in ("format", "model", "serial", "files")}
        self.assertEqual(hashlib.sha256(canonical(metadata)).hexdigest(), self.old["revision"])
        again = self.work / "again.fat"
        sdmedia.make_fat(self.files, again)
        with (self.work / "original.img").open("rb") as stream:
            self.assertEqual(Fat(stream, STARTS[2] * SECTOR).metadata(), self.old)
        with again.open("rb") as stream:
            self.assertEqual(Fat(stream).metadata(), self.old)


class BootIdentityTests(unittest.TestCase):
    def test_native_boot_never_looks_for_a_card(self):
        self.assertIsNone(sdclient.boot_identity("root=/dev/nfs boot=fleet", Path("absent")))

    def test_firmware_and_loader_identity_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "chosen/bootloader").mkdir(parents=True)
            (tree / "chosen/bootloader/partition").write_bytes((2).to_bytes(4, "big"))
            (tree / "chosen/bootloader/tryboot").write_bytes(bytes(4))
            (tree / "serial-number").write_bytes(b"000000001234abcd\0")
            cmdline = "fleet.sd=2 fleet.card=" + "a" * 32 + " fleet.slot=2 fleet.sd_model=pi4"
            self.assertEqual(sdclient.boot_identity(cmdline, tree)["serial"], "1234abcd")
            with self.assertRaises(ValueError):
                sdclient.boot_identity(cmdline.replace("slot=2", "slot=3"), tree)
