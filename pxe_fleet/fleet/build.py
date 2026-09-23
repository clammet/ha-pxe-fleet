"""Offline OS construction. Nothing here writes to an exported generation."""
from contextlib import contextmanager, ExitStack
import json
import logging
from pathlib import Path
import platform
import shutil
import tempfile

from . import images
from .util import atomic_write, capture, digest, run, write_json

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LOG = logging.getLogger(__name__)
PACKAGES = ("initramfs-tools", "busybox", "nfs-common", "python3", "ca-certificates", "gnupg", "podman", "openssh-server", "util-linux", "e2fsprogs")


def remove_private_tree(path):
    mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    if any(p == str(path) or p.startswith(str(path) + "/") for p in mounts):
        raise RuntimeError(f"Refusing to remove a private build/cache with live mounts: {path}")
    shutil.rmtree(path)


def put(root, name, value, mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    atomic_write(path, value, mode)


def disable(root, units):
    for name in units:
        path = root / "etc/systemd/system" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to("/dev/null")


def enable(root, unit, target="multi-user.target"):
    link = root / "etc/systemd/system" / (target + ".wants") / unit
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to("../" + unit)


def ensure_emulation(arch="arm64"):
    if arch == "arm64" and platform.machine() == "aarch64":
        return
    if platform.machine() not in ("x86_64", "aarch64"):
        raise RuntimeError("The builder requires an ARM64 or x86-64 Linux host")
    path = Path("/proc/sys/fs/binfmt_misc")
    if not (path / "register").exists():
        run(["mount", "-t", "binfmt_misc", "binfmt_misc", path])
    # Register only our interpreter, with F so it remains available inside chroots.
    target = {"arm64": "aarch64", "armhf": "arm"}[arch]
    entry = path / ("pxe-fleet-" + target)
    if not entry.exists():
        interpreter = f"/usr/bin/qemu-{target}-static"
        magic = b"\x7fELF" + (b"\x02" if arch == "arm64" else b"\x01") + b"\x01\x01" + b"\x00" * 9 + b"\x02\x00" + (b"\xb7\x00" if arch == "arm64" else b"\x28\x00")
        mask = b"\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff"
        with (path / "register").open("wb") as handle:
            handle.write(b":pxe-fleet-" + target.encode() + b":M::" + magic + b":" + mask + b":" + interpreter.encode() + b":F")


@contextmanager
def chroot_mounts(root):
    with ExitStack() as stack:
        for name, source, fs in (("dev", "/dev", None), ("proc", "proc", "proc"), ("sys", "/sys", None), ("run", "tmpfs", "tmpfs")):
            target = root / name
            target.mkdir(parents=True, exist_ok=True)
            run(["mount", "-t", fs, source, target] if fs else ["mount", "--rbind", source, target])
            stack.callback(run, ["umount", "--recursive", target])
            run(["mount", "--make-rslave", target])
        yield


def chroot(root, *args, output=False):
    command = ["chroot", root, "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "LC_ALL=C", "SYSTEMD_OFFLINE=1", *args]
    return capture(command) if output else run(command)


def source_revision():
    sources = [*ASSETS.glob("*"), *Path(__file__).parent.glob("*.py"),
               *(ASSETS.parent / "bootloader").glob("*.cmd"), *(ASSETS.parent / "bootloaders").glob("*/u-boot.bin")]
    return digest({str(p.relative_to(ASSETS.parent)): images.file_hash(p) for p in sorted(sources) if p.is_file()})


def install_initramfs(root):
    put(root, "etc/initramfs-tools/conf.d/fleet", "BOOT=fleet\nMODULES=most\nBUSYBOX=y\nCOMPRESS=gzip\n")
    put(root, "etc/initramfs-tools/conf.d/resume", "RESUME=none\n")
    for source, target in (
        ("fleet-nfs", "etc/initramfs-tools/scripts/fleet"),
        ("fleet-hook", "etc/initramfs-tools/hooks/fleet"),
        ("fleet-nfsmount", "usr/local/lib/fleet-nfsmount"),
    ):
        put(root, target, (ASSETS / source).read_bytes(), 0o755)
    (root / "etc/initramfs-tools/scripts/init-bottom/fleet-overlay").unlink(missing_ok=True)


def build_base(release, root, cache, scratch, arch="arm64"):
    ensure_emulation(arch)
    images.extract(images.image_file(release, cache), root, scratch)
    return update_base(release, root, arch)


def needs_upgrade(root, arch):
    """Refresh only APT metadata on an unexported cache, then resolve the plan."""
    ensure_emulation(arch)
    put(root, "etc/resolv.conf", Path("/etc/resolv.conf").read_bytes())
    with chroot_mounts(root):
        chroot(root, "apt-get", "update", "--error-on=any")
        plans = [chroot(root, "apt-get", "--simulate", "dist-upgrade", output=True),
                 chroot(root, "apt-get", "--simulate", "--no-install-recommends", "install", *PACKAGES, output=True)]
    return any(line.startswith(("Inst ", "Remv ", "Conf ")) for plan in plans for line in plan.splitlines())


def update_base(release, root, arch="arm64", refresh=True):
    """Apply packages locally to a private copy, never to a client's NFS root."""
    ensure_emulation(arch)
    put(root, "usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n", 0o755)
    put(root, "etc/resolv.conf", Path("/etc/resolv.conf").read_bytes())
    put(root, "etc/fstab", "# Mounts are managed by PXE Fleet.\n")
    put(root, "etc/initramfs-tools/conf.d/fleet", "BOOT=nfs\nMODULES=most\nBUSYBOX=y\nCOMPRESS=gzip\n")
    put(root, "etc/initramfs-tools/conf.d/resume", "RESUME=none\n")
    with chroot_mounts(root):
        if refresh:
            chroot(root, "apt-get", "update", "--error-on=any")
        chroot(root, "apt-get", "-y", "-o", "Dpkg::Options::=--force-confold", "dist-upgrade")
        chroot(root, "apt-get", "-y", "--no-install-recommends", "install", *PACKAGES)
        install_initramfs(root)
        if chroot(root, "dpkg", "--print-architecture", output=True) != arch:
            raise RuntimeError(f"Expected an {arch} root image")
        versions = chroot(root, "dpkg-query", "-W", "-f=${Package}=${Version}\n", output=True)
        for flavor in (("v8", "2712") if arch == "arm64" else ("v6", "v7")):
            kernels = sorted(p.name for p in (root / "lib/modules").iterdir() if p.name.endswith("-rpi-" + flavor))
            if not kernels:
                raise RuntimeError(f"No installed Raspberry Pi {flavor} kernel")
            # dpkg version ordering handles 6.9 -> 6.10 correctly.
            kernel = kernels[0]
            for candidate in kernels[1:]:
                import subprocess
                if subprocess.run(["dpkg", "--compare-versions", candidate, "gt", kernel]).returncode == 0:
                    kernel = candidate
            boot = root / "boot/firmware"
            kernel_image = root / "boot" / ("vmlinuz-" + kernel)
            if not kernel_image.is_file():
                raise RuntimeError(f"Kernel package did not provide {kernel_image.name}")
            shutil.copy2(kernel_image, boot / ("fleet-kernel-" + flavor))
            chroot(root, "mkinitramfs", "-o", "/boot/firmware/fleet-initrd-" + flavor, kernel)
        # Never expose image-provided login credentials or clone SSH identities.
        chroot(root, "usermod", "--password", "!", "root")
        for entry in (root / "etc/shadow").read_text().splitlines():
            user, password, *_ = entry.split(":")
            if password and not password.startswith(("!", "*")):
                chroot(root, "usermod", "--password", "!", user)
        chroot(root, "apt-get", "clean")
    for key in (root / "etc/ssh").glob("ssh_host_*"):
        key.unlink()
    put(root, "etc/machine-id", "")
    (root / "var/lib/dbus/machine-id").unlink(missing_ok=True)
    disable(root, ["apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service", "dphys-swapfile.service", "rpi-resize.service", "resize2fs_once.service", "userconfig.service", "regenerate_ssh_host_keys.service", "NetworkManager.service", "NetworkManager-wait-online.service", "dhcpcd.service", "systemd-resolved.service", "podman-auto-update.timer"])
    put(root, "etc/systemd/network/10-fleet.network", "[Match]\nName=eth* en*\n[Network]\nDHCP=ipv4\nKeepConfiguration=yes\n[DHCPv4]\nUseDNS=no\nUseMTU=no\n")
    chroot(root, "systemctl", "enable", "systemd-networkd.service")
    put(root, "etc/systemd/journald.conf.d/fleet.conf", "[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n")
    put(root, "etc/systemd/system.conf.d/fleet.conf", "[Manager]\nRuntimeWatchdogSec=30s\nRebootWatchdogSec=5min\n")
    # networkd renews the reserved lease while retaining the initramfs address.
    put(root, "etc/containers/storage.conf", '[storage]\ndriver="overlay"\nrunroot="/run/containers/storage"\ngraphroot="/var/lib/containers/storage"\n')
    return digest({"image_sha256": release["sha256"], "packages": sorted(versions.splitlines()), "builder": source_revision()})


def prepare_client(root, spec, generation, token, data_export):
    from .client import render_quadlet
    config = {**spec, "generation": generation, "token": token}
    put(root, "etc/pxe-fleet.json", json.dumps(config), 0o600)
    put(root, "usr/local/lib/pxe-fleet-client.py", Path(__file__).with_name("client.py").read_bytes(), 0o755)
    for source, target in (("sdclient.py", "fleet_sd.py"), ("sd_layout.py", "fleet_sd_layout.py")):
        put(root, "usr/local/lib/" + target, Path(__file__).with_name(source).read_bytes())
    put(root, "etc/hostname", spec["hostname"] + "\n")
    put(root, "etc/hosts", f"127.0.0.1 localhost\n127.0.1.1 {spec['hostname']}\n::1 localhost\n")
    put(root, "etc/resolv.conf", "".join(f"nameserver {ip}\n" for ip in spec["dns"]))
    mounts = f"{spec['server_ip']}:{data_export} /appdata nfs4 rw,hard,vers=4.1,proto=tcp,_netdev,noatime 0 0\n"
    if spec["containers"]:
        # ext4 supplies OverlayFS/xattr semantics; every backing byte lives on
        # the NFS server. systemd orders shutdown before unmounting appdata.
        mounts += "/appdata/.fleet/podman.ext4 /var/lib/containers ext4 loop,_netdev,noatime,x-systemd.requires-mounts-for=/appdata 0 0\n"
    put(root, "etc/fstab", mounts)
    for path in ("appdata", "var/lib/containers"):
        (root / path).mkdir(parents=True, exist_ok=True)
    for asset in ("fleet-agent.service", "fleet-prepare.service"):
        put(root, "etc/systemd/system/" + asset, (ASSETS / asset).read_bytes())
        enable(root, asset)
    for app in spec["containers"]:
        put(root, "etc/containers/systemd/fleet-" + app["name"] + ".container", render_quadlet(app))
        put(root, "etc/pxe-fleet-env/" + app["name"], "".join(k + "=" + v + "\n" for k, v in app["environment"].items()), 0o600)
    for service in spec["apt"]["services"]:
        put(root, "etc/systemd/system/" + service + ".d/fleet.conf", "[Unit]\nRequires=fleet-prepare.service\nAfter=fleet-prepare.service\n")
    # SSH identity persists separately from disposable OS generations.
    put(root, "etc/ssh/sshd_config.d/00-fleet.conf", "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nHostKey /appdata/.fleet/ssh/ssh_host_ed25519_key\n")
    put(root, "etc/systemd/system/ssh.service.d/fleet.conf", "[Unit]\nRequires=fleet-prepare.service\nAfter=fleet-prepare.service\n")
    if spec["ssh_authorized_keys"]:
        put(root, "root/.ssh/authorized_keys", "\n".join(spec["ssh_authorized_keys"]) + "\n", 0o600)
        (root / "root/.ssh").chmod(0o700)
        link = root / "etc/systemd/system/multi-user.target.wants/ssh.service"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to("/usr/lib/systemd/system/ssh.service")
        disable(root, ["ssh.socket"])
    else:
        disable(root, ["ssh.service", "ssh.socket"])


def stage_applications(root, spec):
    if not any(spec["apt"][key] for key in ("packages", "sources", "users")):
        return
    resolver = (root / "etc/resolv.conf").read_bytes()
    put(root, "etc/resolv.conf", Path("/etc/resolv.conf").read_bytes())
    try:
        with chroot_mounts(root):
            chroot(root, "python3", "/usr/local/lib/pxe-fleet-client.py", "stage")
    finally:
        put(root, "etc/resolv.conf", resolver)


class Builder:
    def __init__(self, storage):
        self.storage = storage

    def clean_stale(self):
        mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()]
        candidates = [*(self.storage / "build").glob("base-*"), *(self.storage / "generations").glob("*/.stage-*"),
                      *(self.storage / "bases").glob("*/*/.stage-*")]
        for path in candidates:
            if any(p == str(path) or p.startswith(str(path) + "/") for p in mounts):
                raise RuntimeError(f"Stale build still has mounts; refusing cleanup: {path}")
            if path.is_dir():
                shutil.rmtree(path)

    @contextmanager
    def base(self, release, arch="arm64"):
        cache = self.storage / "cache" / arch
        cache.mkdir(parents=True, exist_ok=True)
        # Persist the verified upstream hash separately from the package revision.
        # Equal hashes never enter image_file/extract/build_base again.
        directory = self.storage / "bases" / arch / release["sha256"]
        directory.mkdir(parents=True, exist_ok=True)
        pointer = directory / "current.json"
        current = json.loads(pointer.read_text()) if pointer.exists() else None
        previous = directory / current["fingerprint"] if current else None
        if previous and not (previous / "root").is_dir():
            raise RuntimeError("Prepared base cache is missing its root; repair the cache before updating")
        revision = source_revision()
        if previous:
            changed = needs_upgrade(previous / "root", arch)
            if not changed and current["builder"] == revision:
                LOG.info("Reusing prepared %s base: upstream image and installed packages unchanged", arch)
                yield previous / "root", current["fingerprint"]
                return
        work = Path(tempfile.mkdtemp(prefix=".stage-", dir=directory))
        try:
            root = work / "root"
            if previous:
                LOG.info("Updating cached %s OS locally; upstream image unchanged", arch)
                run(["cp", "-a", "--reflink=auto", previous / "root", root])
                fingerprint = update_base(release, root, arch, refresh=False)
            else:
                LOG.info("New upstream image checksum: building %s from %s", arch, release["url"])
                fingerprint = build_base(release, root, cache, work, arch)
            final = directory / fingerprint
            if not final.exists():
                run(["sync", "-f", work])
                work.rename(final)
            write_json(pointer, {"image_sha256": release["sha256"], "fingerprint": fingerprint, "builder": revision})
            # No cached base is exported. Commit its pointer before retiring old
            # versions, so interruption always leaves a complete selected root.
            for old in directory.iterdir():
                if old.is_dir() and old != final and not old.name.startswith("."):
                    remove_private_tree(old)
            for old in directory.parent.iterdir():
                if old.is_dir() and old != directory:
                    remove_private_tree(old)
            for file in cache.iterdir():
                if file.is_file() and file.stem != release["sha256"]:
                    file.unlink()
            yield final / "root", fingerprint
        finally:
            # Never recursively remove a chroot with live bind mounts, including
            # when an unmount failed. TemporaryDirectory's exit finalizer is unsafe
            # for mount trees because it also runs on interpreter shutdown.
            mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()]
            if any(p == str(work) or p.startswith(str(work) + "/") for p in mounts):
                LOG.error("Retaining build directory with live mounts: %s", work)
            elif work.exists():
                shutil.rmtree(work)
