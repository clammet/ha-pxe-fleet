"""Exercise real NFSv4, writable overlays and TFTP in an isolated container."""
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pxe_fleet"))
from fleet.config import validate
from fleet.server import Services
from fleet.state import Store
from fleet.util import write_json, run

ip = subprocess.check_output(["hostname", "-I"], text=True).split()[0]
cfg = validate({"server_ip": ip, "clients": [{"serial": "12345678", "ip": "192.0.2.2", "hostname": "test", "model": "pi4"}]})
store = Store(Path("/data/network-test"))
store.register(cfg)
store.state["clients"]["12345678"]["client"]["ip"] = ip
generation = "a" * 24
root = store.root("12345678", generation)
root.mkdir(parents=True, exist_ok=True)
(root / "immutable").write_text("original")
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
        f'-o vers=4.1,ro {ip}:/12345678/roots/{late} /mnt/late; }}\n')
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
    for name in ("lower", "upper", "merged", "data"):
        Path("/mnt/" + name).mkdir(exist_ok=True)
    run(["mount", "-t", "nfs", "-o", "vers=4.1,ro", f"{ip}:/12345678/roots/{generation}", "/mnt/lower"])
    mounts.append("/mnt/lower")
    run(["mount", "-t", "tmpfs", "tmpfs", "/mnt/upper"])
    mounts.append("/mnt/upper")
    Path("/mnt/upper/rw").mkdir()
    Path("/mnt/upper/work").mkdir()
    run(["mount", "-t", "overlay", "overlay", "-o", "lowerdir=/mnt/lower,upperdir=/mnt/upper/rw,workdir=/mnt/upper/work", "/mnt/merged"])
    mounts.append("/mnt/merged")
    Path("/mnt/merged/immutable").write_text("RAM change")
    assert (root / "immutable").read_text() == "original"
    data = store.path / "appdata/12345678"
    run(["mount", "-t", "nfs", "-o", "vers=4.1,rw", f"{ip}:/12345678/appdata", "/mnt/data"])
    mounts.append("/mnt/data")
    Path("/mnt/data/persistent").write_text("saved")
    assert (data / "persistent").read_text() == "saved"
    # Exercise the actual initramfs transition, including moving backing mounts
    # below the overlay root. Only module loading is stubbed: OverlayFS is already
    # loaded above, while this container has no host kernel module directory.
    Path("/mnt/bootroot").mkdir(exist_ok=True)
    run(["mount", "-t", "nfs", "-o", "vers=4.1,ro", f"{ip}:/12345678/roots/{generation}", "/mnt/bootroot"])
    mounts.append("/mnt/bootroot")
    Path("/scripts").mkdir(exist_ok=True)
    Path("/scripts/functions").write_text('panic() { echo "$*" >&2; exit 1; }\n')
    Path("/tmp/helpers").mkdir(exist_ok=True)
    Path("/tmp/helpers/modprobe").write_text("#!/bin/sh\nexit 0\n")
    Path("/tmp/helpers/modprobe").chmod(0o755)
    import os
    run(["sh", "/workspace/pxe_fleet/assets/fleet-overlay"], env={**os.environ, "rootmnt": "/mnt/bootroot", "PATH": "/tmp/helpers:" + os.environ["PATH"]})
    Path("/mnt/bootroot/immutable").write_text("boot overlay")
    assert (root / "immutable").read_text() == "original"
    (store.path / "tftp/probe").write_text("boot payload")
    result = subprocess.check_output(["curl", "--fail", "--silent", f"tftp://{ip}/probe"], text=True)
    assert result == "boot payload"
    print("Late NFS retry, NFSv4 read-only root, RAM overlay, persistent data, and TFTP passed", flush=True)
finally:
    if retry is not None and retry.poll() is None:
        retry.kill()
        retry.wait()
    for mount in reversed(mounts):
        run(["umount", "--recursive", mount])
    services.close()
    run(["rpc.nfsd", "0"])
