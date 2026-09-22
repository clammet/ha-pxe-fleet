#!/usr/bin/env python3
"""Standalone Pi agent; copied into each OS generation (standard library only)."""
import concurrent.futures
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

LOG = logging.getLogger("fleet-client")
DATA = Path("/appdata")


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(args):
    return run(args, stdout=subprocess.PIPE, text=True).stdout.strip()


def unit_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_quadlet(app):
    lines = ["[Unit]", "Description=Fleet container " + app["name"],
             "Requires=fleet-prepare.service", "After=fleet-prepare.service",
             "RequiresMountsFor=/appdata /var/lib/containers", "", "[Container]",
             "Image=" + app["image"], "ContainerName=fleet-" + app["name"],
             "PodmanArgs=--authfile=/appdata/.fleet/registry-auth.json",
             "AutoUpdate=registry", "Pull=missing", "LogDriver=journald",
             "Network=" + app.get("network", "bridge"),
             "EnvironmentFile=/etc/pxe-fleet-env/" + app["name"]]
    for volume in app.get("volumes", []):
        lines.append(f"Volume=/appdata/{volume['source']}:{volume['target']}" + (":ro" if volume.get("read_only") else ":rw"))
    lines.extend("PublishPort=" + p for p in app.get("ports", []))
    lines.extend("AddDevice=" + d for d in app.get("devices", []))
    if app.get("command"):
        lines.append("Exec=" + " ".join(unit_quote(a) for a in app["command"]))
    lines.extend(["", "[Service]", "Restart=always", "RestartSec=10", "TimeoutStartSec=900", "", "[Install]", "WantedBy=multi-user.target", ""])
    return "\n".join(lines)


def require_appdata():
    result = json.loads(output(["findmnt", "--json", "--mountpoint", str(DATA), "-o", "FSTYPE,TARGET"]))
    if not result.get("filesystems") or result["filesystems"][0]["fstype"] not in ("nfs", "nfs4"):
        raise RuntimeError("Refusing to start applications without the persistent NFS mount")


def data_directory(relative, uid=0, gid=0, seed=None):
    path = DATA / relative
    # App data is writable by applications, so it may contain hostile symlinks.
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA.resolve()) or resolved == DATA.resolve():
        raise RuntimeError("Persistent directory escapes appdata")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".fleet-seed-", dir=path.parent))
        try:
            if seed:
                run(["cp", "-a", str(seed) + "/.", str(temporary)])
            os.chown(temporary, uid, gid)
            temporary.chmod(0o750)
            temporary.rename(path)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    elif path.stat().st_uid != uid or path.stat().st_gid != gid:
        raise RuntimeError(f"{relative}: persistent UID/GID differs from configuration; migrate ownership explicitly")
    return path


def configure_sources(spec):
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, _, value = line.partition("=")
        release[key] = value.strip('"')
    codename = release["VERSION_CODENAME"]
    keydir = Path("/etc/apt/keyrings")
    keydir.mkdir(exist_ok=True)
    for source in spec["apt"]["sources"]:
        request = urllib.request.Request(source["key_url"], headers={"User-Agent": "pxe-fleet/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            if not response.url.startswith("https://"):
                raise RuntimeError("APT key redirected away from HTTPS")
            key = response.read(1024 * 1024 + 1)
        if len(key) > 1024 * 1024 or hashlib.sha256(key).hexdigest() != source["key_sha256"].lower():
            raise RuntimeError("APT signing-key checksum mismatch")
        keyfile = keydir / ("fleet-" + source["name"] + ".gpg")
        run(["gpg", "--batch", "--yes", "--dearmor", "--output", str(keyfile)], input=key)
        keyfile.chmod(0o644)
        suites = source["suites"].replace("{codename}", codename)
        Path("/etc/apt/sources.list.d/fleet-" + source["name"] + ".sources").write_text(
            f"Types: deb\nURIs: {source['url']}\nSuites: {suites}\nComponents: {source.get('components', 'main')}\nSigned-By: {keyfile}\n")
    Path("/etc/apt/preferences.d/fleet-kernel").write_text(
        "Package: linux-image-* linux-headers-* raspi-firmware raspberrypi-kernel* raspberrypi-bootloader*\nPin: version *\nPin-Priority: -1\n")


def package_versions():
    return output(["dpkg-query", "-W", "-f=${Package}=${Version}\n"])


def apt_install(spec, initial=False):
    packages = spec["apt"]["packages"]
    if not packages:
        return False
    with open("/run/fleet-apt.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
        before = package_versions()
        run(["apt-get", "update", "--error-on=any"], env=env, timeout=600)
        # Recover dpkg state after an interrupted installation before applying
        # desired packages again. policy-rc.d still prevents premature starts.
        try:
            run(["dpkg", "--configure", "-a"], env=env, timeout=1200)
        except subprocess.CalledProcessError:
            run(["apt-get", "-y", "-f", "install"], env=env, timeout=1200)
        command = ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold", "install"]
        run(command + packages, env=env, timeout=1200)
        run(["apt-get", "clean"], env=env, timeout=60)
        return before != package_versions()


def prepare_accounts(spec):
    for user in spec["apt"].get("users", []):
        import pwd
        import grp
        try:
            group = grp.getgrnam(user["name"])
            if group.gr_gid != user["gid"]:
                raise RuntimeError("Configured service group has a different GID")
        except KeyError:
            run(["groupadd", "--system", "--gid", str(user["gid"]), user["name"]])
        try:
            account = pwd.getpwnam(user["name"])
            if (account.pw_uid, account.pw_gid) != (user["uid"], user["gid"]):
                raise RuntimeError("Configured service user has different UID/GID")
        except KeyError:
            run(["useradd", "--system", "--uid", str(user["uid"]), "--gid", str(user["gid"]), "--no-create-home", "--shell", "/usr/sbin/nologin", user["name"]])


def stage(spec):
    """Run only in the builder's private chroot, before it is exported."""
    prepare_accounts(spec)
    configure_sources(spec)
    apt_install(spec, initial=True)
    Path("/etc/fleet-apps-staged").write_text("ready\n")


def prepare(spec):
    require_appdata()
    prepare_accounts(spec)
    state = data_directory(".fleet")
    state.chmod(0o700)
    registry_auth = state / "registry-auth.json"
    if not registry_auth.exists():
        registry_auth.write_text('{"auths":{}}\n')
        registry_auth.chmod(0o600)
    ssh = data_directory(".fleet/ssh")
    hostkey = ssh / "ssh_host_ed25519_key"
    if not hostkey.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)])
    for bind in spec["apt"]["persistent"]:
        target = Path(bind["target"])
        if target.is_symlink():
            raise RuntimeError("Refusing to mount over a symlink")
        target.mkdir(parents=True, exist_ok=True)
        source = data_directory(bind["source"], bind["uid"], bind["gid"], seed=target)
        if subprocess.run(["mountpoint", "-q", str(target)]).returncode:
            run(["mount", "--bind", str(source), str(target)])
    for app in spec["containers"]:
        for volume in app["volumes"]:
            data_directory(volume["source"], volume.get("uid", 0), volume.get("gid", 0))
    if not Path("/etc/fleet-apps-staged").exists():
        configure_sources(spec)
        apt_install(spec, initial=True)
    run(["systemctl", "daemon-reload"])
    # Use --no-block: these services have After=fleet-prepare and cannot start
    # until this oneshot has returned. Waiting here would deadlock boot.
    for service in spec["apt"]["services"]:
        run(["systemctl", "enable", service])
        run(["systemctl", "--no-block", "start", service])
    for app in spec["containers"]:
        run(["systemctl", "--no-block", "start", "fleet-" + app["name"] + ".service"])


def healthy(spec):
    services = ["fleet-prepare.service", *spec["apt"]["services"], *("fleet-" + a["name"] + ".service" for a in spec["containers"])]
    return all(subprocess.run(["systemctl", "is-active", "--quiet", s]).returncode == 0 for s in services)


def exchange(spec, status):
    nonce = secrets.token_hex(24)
    data = json.dumps({**status, "nonce": nonce}, sort_keys=True, separators=(",", ":")).encode()
    token = spec["token"].encode()
    signature = hmac.new(token, data, hashlib.sha256).hexdigest()
    url = f"http://{spec['server_ip']}:{spec['control_port']}/v1/clients/{spec['serial']}"
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "X-Fleet-Signature": signature})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(65537)
        expected = hmac.new(token, payload, hashlib.sha256).hexdigest()
        if len(payload) > 65536 or not hmac.compare_digest(response.headers.get("X-Fleet-Signature", ""), expected):
            raise RuntimeError("Invalid controller response signature")
    result = json.loads(payload)
    if result.get("nonce") != nonce:
        raise RuntimeError("Replayed controller response")
    return result


def update_apps(spec):
    if apt_install(spec):
        for service in spec["apt"]["services"]:
            run(["systemctl", "restart", service], timeout=180)
    if spec["containers"]:
        run(["podman", "auto-update", "--rollback=true", "--authfile=/appdata/.fleet/registry-auth.json"], timeout=1800)


def agent(spec):
    if __package__:
        from .sdclient import detect
    else:
        from fleet_sd import detect
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    sd = detect(spec, boot_id)
    next_update = time.monotonic()
    future = None
    error = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    healthy_since = None
    while True:
        try:
            ready = healthy(spec)
            healthy_since = (healthy_since or time.monotonic()) if ready else None
            if future and future.done():
                try:
                    future.result()
                    error = None
                except Exception as exc:
                    error = str(exc)
                    LOG.exception("Application update failed; will retry")
                future = None
            # The local deadline also works if the control endpoint disappears
            # after a firmware-only trial. Finish any in-flight app update first.
            if sd and future is None and sd.expire_trial():
                run(["systemctl", "reboot"], timeout=30)
                time.sleep(60)
                continue
            stable = bool(healthy_since and time.monotonic() - healthy_since >= 60)
            status = {"generation": spec["generation"], "boot_id": boot_id,
                      "healthy": stable and not (sd and sd.trial),
                      "update_error": error, "updating": future is not None}
            if sd:
                status["sd"] = sd.report()
            reply = exchange(spec, status)
            action = sd.handle(reply, stable) if sd and future is None else ("wait" if future else "ready")
            if action in ("tryboot", "reboot") or reply.get("reboot") and action == "ready":
                LOG.info("Rebooting into generation %s", reply["desired"])
                run(["systemctl", "reboot"] + (["--reboot-argument=0 tryboot"] if action == "tryboot" else []), timeout=30)
                time.sleep(60)
            if ready and future is None and time.monotonic() >= next_update:
                future = executor.submit(update_apps, spec)
                next_update = time.monotonic() + spec["app_update_minutes"] * 60
        except Exception:
            LOG.exception("Fleet reconciliation failed; running applications are retained")
        time.sleep(30)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    spec = json.loads(Path("/etc/pxe-fleet.json").read_text())
    {"prepare": prepare, "agent": agent, "stage": stage}[sys.argv[1]](spec)


if __name__ == "__main__":
    main()
