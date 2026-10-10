"""Exercise writable NFS roots, NFS-backed Podman storage and TFTP in isolation."""
from pathlib import Path
import subprocess
import sys
import time
import tarfile
import os
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pxe_fleet"))
from fleet.config import validate
from fleet.server import Services
from fleet.state import Store
from fleet.util import write_json, run

ip = subprocess.check_output(["hostname", "-I"], text=True).split()[0]
cfg = validate({"server_ip": ip, "clients": [{"serial": "12345678", "ip": "192.0.2.2", "hostname": "test", "model": "pi4", "container_storage_gib": 1, "containers": [{"name": "test", "image": "docker.io/library/busybox:latest"}]}]})
store = Store(Path("/data/network-test"))
store.register(cfg)
store.state["clients"]["12345678"]["client"]["ip"] = ip
generation = "a" * 24
root = store.root("12345678", generation)
root.mkdir(parents=True, exist_ok=True)
(root / "system-file").write_text("original")
write_json(root.parent / "manifest.json", {"client": cfg["clients"][0]})
services = Services(store, cfg)
mounts = []
retry = None
try:
    services.start()
    time.sleep(2)
    services.check()
    services.close()
    services = Services(store, cfg)
    services.start()
    services.check()
    # TFTP/controller startup can precede NFS exports. Exercise the actual retry
    # driver against a missing export, then make it available without restarting
    # the client process. Only distro DHCP discovery is stubbed in this container.
    late = "b" * 24
    lateroot = store.root("12345678", late)
    lateroot.mkdir(parents=True, exist_ok=True)
    (lateroot / "ready").write_text("late NFS export")
    Path("/mnt/late").mkdir(exist_ok=True)
    Path("/scripts").mkdir(exist_ok=True)
    Path("/scripts/nfs").write_text(
        'nfs_top() { :; }\nmodprobe() { :; }\nwait_for_udev() { :; }\n'
        'nfs_mount_root_impl() { sh /workspace/pxe_fleet/assets/fleet-nfsmount '
        f'-o vers=4.1,rw,hard {ip}:/12345678/roots/{late} /mnt/late; }}\n')
    retry = subprocess.Popen(["sh", "-c", ". /workspace/pxe_fleet/assets/fleet-nfs; mountroot"], stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    assert retry.poll() is None, "Client did not keep waiting for NFS"
    write_json(lateroot.parent / "manifest.json", {"client": cfg["clients"][0]})
    services.exports()
    mounts.append("/mnt/late")
    _, retry_log = retry.communicate(timeout=45)
    assert retry.returncode == 0, retry_log
    assert "retrying" in retry_log
    assert Path("/mnt/late/ready").read_text() == "late NFS export"
    for name in ("os", "data", "containers"):
        Path("/mnt/" + name).mkdir(exist_ok=True)
    run(["mount", "-t", "nfs", "-o", "vers=4.1,rw,hard", f"{ip}:/12345678/roots/{generation}", "/mnt/os"])
    mounts.append("/mnt/os")
    with Path("/mnt/os/system-file").open("w") as stream:
        stream.write("installed package update")
        stream.flush()
        os.fsync(stream.fileno())
    assert (root / "system-file").read_text() == "installed package update"
    run(["umount", "/mnt/os"])
    mounts.remove("/mnt/os")
    run(["mount", "-t", "nfs", "-o", "vers=4.1,rw,hard", f"{ip}:/12345678/roots/{generation}", "/mnt/os"])
    mounts.append("/mnt/os")
    assert Path("/mnt/os/system-file").read_text() == "installed package update"
    data = store.path / "appdata/12345678"
    run(["mount", "-t", "nfs", "-o", "vers=4.1,rw,hard", f"{ip}:/12345678/appdata", "/mnt/data"])
    mounts.append("/mnt/data")
    Path("/mnt/data/persistent").write_text("saved")
    assert (data / "persistent").read_text() == "saved"
    # This is the real proposed storage path: the loop backing file is opened
    # through NFS, never through the server's local path.
    image = Path("/mnt/data/.fleet/podman.ext4")
    run(["mount", "-t", "ext4", "-o", "loop,noatime", image, "/mnt/containers"])
    mounts.append("/mnt/containers")
    probe = Path("/mnt/containers/probe")
    probe.write_text("survives unmount")
    os.setxattr(probe, "user.fleet-test", b"metadata")
    # Build a minimal native image without any registry dependency.
    with tarfile.open("/tmp/container.tar", "w") as archive:
        archive.add("/bin/busybox", arcname="bin/busybox")
    podman = ["podman", "--root", "/mnt/containers/storage", "--runroot", "/run/fleet-storage-test",
              "--storage-driver", "overlay", "--cgroup-manager", "cgroupfs", "--events-backend", "file"]
    run(podman + ["import", "/tmp/container.tar", "localhost/fleet-test:latest"])
    run(podman + ["run", "--name", "fleet-persistence", "--network", "none", "--cgroups", "disabled",
                  "--security-opt", "seccomp=unconfined", "localhost/fleet-test:latest", "/bin/busybox",
                  "sh", "-c", "echo container-write > /marker"])
    run(podman + ["commit", "fleet-persistence", "localhost/fleet-saved:latest"])
    run(podman + ["rm", "fleet-persistence"])
    run(["umount", "/mnt/containers"])
    mounts.remove("/mnt/containers")
    run(["mount", "-t", "ext4", "-o", "loop,noatime", image, "/mnt/containers"])
    mounts.append("/mnt/containers")
    assert probe.read_text() == "survives unmount"
    assert os.getxattr(probe, "user.fleet-test") == b"metadata"
    result = subprocess.check_output(podman + ["run", "--rm", "--network", "none", "--cgroups", "disabled",
        "--security-opt", "seccomp=unconfined", "localhost/fleet-saved:latest", "/bin/busybox", "cat", "/marker"], text=True)
    assert result.strip() == "container-write"
    (store.path / "tftp/probe").write_text("boot payload")
    result = subprocess.check_output(["curl", "--fail", "--silent", f"tftp://{ip}/probe"], text=True)
    assert result == "boot payload"
    # Create a RAM client and use the actual initramfs copy/unmount helper.
    ramcfg = validate({"server_ip": ip, "clients": [{"serial": "87654321", "ip": "192.0.2.3", "hostname": "ram-test", "model": "pi4", "os_storage": "ram"}]})
    store.register(ramcfg)
    store.state["clients"]["87654321"]["client"]["ip"] = ip
    ramroot = store.root("87654321", generation)
    (ramroot / "bin").mkdir(parents=True)
    shutil.copy2("/bin/busybox", ramroot / "bin/busybox")
    (ramroot / "system-file").write_text("RAM factory version")
    (ramroot / "linked").symlink_to("system-file")
    os.link(ramroot / "system-file", ramroot / "hardlinked")
    (ramroot / "system-file").chmod(0o640)
    write_json(ramroot.parent / "manifest.json", {"client": ramcfg["clients"][0]})
    services.exports()
    shutil.copy2("/bin/cp", "/bin/fleet-copy")
    Path("/mnt/ram").mkdir(exist_ok=True)
    run(["mount", "-t", "nfs", "-o", "vers=4.1,ro,hard", f"{ip}:/87654321/roots/{generation}", "/mnt/ram"])
    mounts.append("/mnt/ram")
    # A too-small root must fail without booting a partial tree or writing NFS.
    failed = subprocess.run(["sh", "/workspace/pxe_fleet/assets/fleet-ram-root", "/mnt/ram", "1M"], capture_output=True)
    assert failed.returncode != 0
    assert subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", "/mnt/ram"], text=True).strip() == "nfs4"
    assert not Path("/mnt/ram.fleet-ram").exists()
    run(["sh", "/workspace/pxe_fleet/assets/fleet-ram-root", "/mnt/ram", "32M"])
    assert subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", "/mnt/ram"], text=True).strip() == "tmpfs"
    ram = Path("/mnt/ram")
    assert (ram / "linked").is_symlink()
    assert (ram / "system-file").stat().st_ino == (ram / "hardlinked").stat().st_ino
    assert (ram / "system-file").stat().st_mode & 0o777 == 0o640
    for name, size in (("appdata", "32M"), ("var/lib/containers", "128M")):
        target = ram / name
        target.mkdir(parents=True)
        run(["mount", "-t", "tmpfs", "-o", "size=" + size, "tmpfs", target])
        mounts.append(str(target))
    ram_podman = ["podman", "--root", str(ram / "var/lib/containers/storage"), "--runroot", "/run/fleet-ram-test",
                  "--storage-driver", "overlay", "--cgroup-manager", "cgroupfs", "--events-backend", "file"]
    run(ram_podman + ["import", "/tmp/container.tar", "localhost/fleet-ram-test:latest"])
    # Disconnect every NFS client mount before stopping the isolated NFS server.
    for target in ("/mnt/containers", "/mnt/data", "/mnt/os", "/mnt/late"):
        run(["umount", target])
        mounts.remove(target)
    services.close()
    run(["rpc.nfsd", "0"])
    assert subprocess.run(["findmnt", "-rn", "-t", "nfs,nfs4"], stdout=subprocess.DEVNULL).returncode == 1
    run(["chroot", ram, "/bin/busybox", "sh", "-c", "echo updated > /system-file; echo runtime-data > /appdata/value; /bin/busybox cat /linked"])
    assert (ramroot / "system-file").read_text() == "RAM factory version"
    result = subprocess.check_output(ram_podman + ["run", "--rm", "--network", "none", "--cgroups", "disabled",
        "--security-opt", "seccomp=unconfined", "-v", str(ram / "appdata") + ":/data", "localhost/fleet-ram-test:latest",
        "/bin/busybox", "sh", "-c", "/bin/busybox cat /data/value; echo from-container > /data/container"], text=True)
    assert result.strip() == "runtime-data"
    assert (ram / "appdata/container").read_text().strip() == "from-container"
    for target in (str(ram / "var/lib/containers"), str(ram / "appdata"), str(ram)):
        run(["umount", target])
        mounts.remove(target)
    # Fresh RAM starts empty; no write-through or hidden synchronization.
    assert not (ram / "system-file").exists()
    assert not (store.path / "appdata/87654321/.fleet/podman.ext4").exists()
    print("NFS persistence/retry/TFTP and complete RAM OS, appdata and Podman after NFS shutdown passed", flush=True)
finally:
    if retry is not None and retry.poll() is None:
        retry.kill()
        retry.wait()
    for mount in reversed(mounts):
        run(["umount", "--recursive", mount])
    services.close()
    run(["rpc.nfsd", "0"])
