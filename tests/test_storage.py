"""Container backing files must persist and must never be reformatted on startup."""
from pathlib import Path
import shutil
import tempfile
import unittest

from fleet.storage import container_disk


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("blkid"), "ext4 tools not installed")
class ContainerDiskTests(unittest.TestCase):
    def test_existing_disk_is_retained_and_wrong_identity_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            client = {"serial": "1234abcd", "containers": [{}], "container_storage_gib": 1}
            container_disk(path, client, "secret")
            disk = path / ".fleet/podman.ext4"
            with disk.open("r+b") as stream:
                stream.write(b"keep boot sector contents")
            before = disk.stat()
            container_disk(path, client, "secret")
            self.assertEqual((disk.stat().st_ino, disk.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
            with self.assertRaisesRegex(RuntimeError, "identity"):
                container_disk(path, client, "wrong")
            with disk.open("rb") as stream:
                self.assertEqual(stream.read(25), b"keep boot sector contents")

    def test_unrecognised_file_and_symlink_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / ".fleet").mkdir()
            other = path / "data"
            other.write_text("precious")
            (path / ".fleet/podman.ext4").symlink_to(other)
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                container_disk(path, {"serial": "1234abcd", "containers": [{}], "container_storage_gib": 1}, "secret")
            self.assertEqual(other.read_text(), "precious")
