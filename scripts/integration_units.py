"""Validate generated units and real client APT installation using a built root.

Run after integration_build.py, using the same disposable test volume.
"""
import json
from pathlib import Path
import shutil
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pxe_fleet"))
from fleet.build import chroot, chroot_mounts, prepare_client, stage_applications
from fleet.config import validate, client_spec
from fleet.util import run

workspace = Path(__file__).resolve().parents[1]
cfg = validate(yaml.safe_load((workspace / "examples/fleet.yaml").read_text()))
spec = client_spec(cfg, cfg["clients"][0])
roots = list(Path("/data/fleet/generations").glob("*/*/root"))
if not roots:
    raise RuntimeError("Run integration_build.py first")
work = Path(tempfile.mkdtemp(prefix="units-", dir="/data"))
root = work / "root"
try:
    run(["cp", "-a", "--reflink=auto", roots[-1], root])
    prepare_client(root, spec, "f" * 24, "test-token", "/1234abcd/appdata")
    # The example deliberately contains documentation-only DNS addresses.
    (root / "etc/resolv.conf").write_bytes(Path("/etc/resolv.conf").read_bytes())
    stage_applications(root, spec)
    with chroot_mounts(root):
        generated = chroot(root, "/usr/lib/systemd/system-generators/podman-system-generator", "--dryrun", output=True)
        assert "fleet-web.service" in generated
        assert "--authfile=/appdata/.fleet/registry-auth.json" in generated
        chroot(root, "systemd-analyze", "verify", "--man=no", "fleet-agent.service", "fleet-prepare.service")
        for name in ("fstab-normal", "fstab-early", "fstab-late"):
            (root / "tmp" / name).mkdir(exist_ok=True)
        chroot(root, "/usr/lib/systemd/system-generators/systemd-fstab-generator",
               "/tmp/fstab-normal", "/tmp/fstab-early", "/tmp/fstab-late")
        mount = (root / "tmp/fstab-normal/var-lib-containers.mount").read_text()
        assert "What=/appdata/.fleet/podman.ext4" in mount
        assert "RequiresMountsFor=/appdata" in mount
        assert "Type=ext4" in mount and "loop" in mount
        # Exercise the same client-side update code again: installed packages
        # should already be current, and the stable service UID must survive.
        chroot(root, "python3", "-c", "import json,runpy; m=runpy.run_path('/usr/local/lib/pxe-fleet-client.py'); s=json.load(open('/etc/pxe-fleet.json')); m['configure_sources'](s); m['apt_install'](s, initial=True)")
        installed = chroot(root, "dpkg-query", "-W", "-f=${Status}", "mosquitto", output=True)
        assert installed == "install ok installed"
        assert chroot(root, "id", "-u", "mosquitto", output=True) == "2100"
    print("Podman Quadlet generation, systemd units, and client APT install passed", flush=True)
finally:
    mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    if not any(p.startswith(str(work) + "/") for p in mounts):
        shutil.rmtree(work)
