# Development

Run the portable tests and configuration validation:

```sh
python3 -m pip install PyYAML
PYTHONPATH=pxe_fleet python3 -m unittest discover -s tests -v
PYTHONPATH=pxe_fleet python3 -m fleet.server --config examples/fleet.yaml --validate
shellcheck pxe_fleet/assets/fleet-hook pxe_fleet/assets/fleet-overlay
docker build -t ha-pxe-fleet:test pxe_fleet
```

Three integration checks use real Linux mounts. Run them in disposable containers,
with isolated networking. Do not add host networking to these test commands.
The image check downloads the official ARM64 Lite image and runs APT; allow
approximately 15 GB of disk space and several minutes. On an x86-64 test host,
it registers a QEMU binfmt interpreter so ARM64 package scripts can execute.

```sh
docker run --rm --privileged \
  --mount type=volume,src=pxe-fleet-validation,dst=/data \
  --mount "type=bind,src=$(pwd),dst=/workspace,readonly" \
  ha-pxe-fleet:test python3 /workspace/scripts/integration_build.py

docker run --rm --privileged \
  --mount type=volume,src=pxe-fleet-network-test,dst=/data \
  --mount "type=bind,src=$(pwd),dst=/workspace,readonly" \
  ha-pxe-fleet:test python3 /workspace/scripts/integration_network.py

docker run --rm --privileged \
  --mount type=volume,src=pxe-fleet-validation,dst=/data \
  --mount "type=bind,src=$(pwd),dst=/workspace,readonly" \
  ha-pxe-fleet:test python3 /workspace/scripts/integration_units.py
```

The network check requires the Linux host's NFS server and OverlayFS support.
It verifies NFSv4 mounts, RAM-only changes to the OS, persistent data writes and
actual TFTP transfers and the real initramfs mount transition. The image check verifies extraction, package updates,
kernel/initramfs construction and generation staging. The units check uses the
real Podman generator and installs an APT application in an unpublished root.
These checks do not substitute
for booting physical Pi 3, 4 and 5 boards under Home Assistant OS.

Remove the disposable test volumes when finished:

```sh
docker volume rm pxe-fleet-validation pxe-fleet-network-test
```

Before a release, test each board, loss of the controller during staging, an
unreachable application repository, reboot during an update, NFS server restart
with an open data file, failed service health, and a changed upstream major OS
release. Verify application data and stable UIDs survive each case. Do not claim
hardware compatibility from unit tests alone.
