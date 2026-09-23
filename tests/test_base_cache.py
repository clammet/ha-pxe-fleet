"""Prepared-base reuse and transactional updates, with no chroot or mounts."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fleet.build import Builder
from fleet.util import digest


class BaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.builder = Builder(self.path)
        self.release = {"url": "https://example.org/lite.img.xz", "sha256": "a" * 64}
        self.revision = patch("fleet.build.source_revision", return_value="builder-1").start()
        self.fresh = patch("fleet.build.build_base", side_effect=self.build).start()
        self.plan = patch("fleet.build.needs_upgrade", return_value=False).start()
        self.update = patch("fleet.build.update_base", side_effect=self.upgrade).start()
        patch("fleet.build.run", side_effect=self.command).start()
        self.addCleanup(patch.stopall)

    def command(self, args):
        if args[0] == "cp":
            shutil.copytree(args[-2], args[-1])
        else:
            self.assertEqual(args[0], "sync")

    def build(self, release, root, cache, scratch, arch):
        root.mkdir()
        (root / "packages").write_text("v1")
        return digest([release["sha256"], "v1", self.revision.return_value])

    def upgrade(self, release, root, arch, refresh):
        self.assertFalse(refresh)
        (root / "packages").write_text("v2")
        return digest([release["sha256"], "v2", self.revision.return_value])

    def test_unchanged_checksum_reuses_prepared_root_across_restart(self):
        with self.builder.base(self.release) as first:
            pass
        # A redirected URL change with identical content must not rebuild.
        with Builder(self.path).base({**self.release, "url": "https://example.org/renamed.img.xz"}) as again:
            self.assertEqual(first, again)
        self.assertEqual(self.fresh.call_count, 1)
        self.update.assert_not_called()
        self.plan.assert_called_once()

    def test_package_update_clones_cached_base_without_extracting_image(self):
        with self.builder.base(self.release) as (old, _):
            pass
        self.plan.return_value = True
        with self.builder.base(self.release) as (updated, _):
            self.assertEqual((updated / "packages").read_text(), "v2")
            self.assertNotEqual(updated, old)
        self.assertEqual(self.fresh.call_count, 1)
        self.update.assert_called_once()

    def test_failed_upgrade_keeps_last_complete_cache(self):
        with self.builder.base(self.release) as before:
            pass
        self.plan.return_value = True
        def fail(release, root, arch, refresh):
            (root / "packages").write_text("half installed")
            raise RuntimeError("APT failed")
        self.update.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "APT failed"), self.builder.base(self.release):
            pass
        self.plan.return_value = False
        with self.builder.base(self.release) as after:
            self.assertEqual(before, after)
            self.assertEqual((after[0] / "packages").read_text(), "v1")

    def test_changed_image_hash_builds_fresh_and_persists_hash(self):
        with self.builder.base(self.release):
            pass
        changed = {**self.release, "sha256": "b" * 64}
        with self.builder.base(changed) as (root, fingerprint):
            pointer = json.loads((root.parent.parent / "current.json").read_text())
            self.assertEqual(pointer["image_sha256"], changed["sha256"])
            self.assertEqual(pointer["fingerprint"], fingerprint)
        self.assertEqual(self.fresh.call_count, 2)
        self.update.assert_not_called()

    def test_builder_change_updates_private_cache_without_image_extraction(self):
        with self.builder.base(self.release):
            pass
        self.revision.return_value = "builder-2"
        with self.builder.base(self.release):
            pass
        self.assertEqual(self.fresh.call_count, 1)
        self.update.assert_called_once()
