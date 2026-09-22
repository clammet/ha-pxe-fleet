# Development

Use Python 3.11 or newer. Run the portable tests and configuration validation:

```sh
python3 -m pip install PyYAML
PYTHONPATH=pxe_fleet python3 -m unittest discover -s tests -v
PYTHONPATH=pxe_fleet python3 -m fleet.server --config examples/fleet.yaml --validate
shellcheck pxe_fleet/assets/fleet-hook pxe_fleet/assets/fleet-overlay pxe_fleet/assets/fleet-nfsmount pxe_fleet/assets/fleet-nfs boot-media/*.sh boot-media/scripts/*.sh pxe_fleet/bootloader/build-tools.sh
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

## SD boot media and 32-bit OS checks

Install `dosfstools`, `mtools` and `u-boot-tools` to include the real FAT image
round-trip and SD update fault checks in the portable suite. Push/PR CI installs
these tools; checks whose required tools are absent are explicitly skipped.

Run `./boot-media/build.sh all` to cross-compile all five loaders. See
[boot-media/README.md](boot-media/README.md) for preparing per-device images.
The manual **Build SD boot media** workflow also builds every variant and uploads
per-model SD image artifacts, with no registry/release publication.

For the new ARMv6/v7 path, run `scripts/integration_build.py --arch armhf` using
the privileged image-check command above and a **different disposable volume**
(e.g. `pxe-fleet-validation-armhf`). It builds the official 32-bit Lite image,
checks both v6/v7 initramfs files and stages a Pi 2 SD/native payload. Remove that
volume when finished. The ARM64 check covers v8/2712 and a Pi 4 SD payload.

Hardware acceptance must cover Pi 1B/B+, Pi 2, Pi 3B/3B+ and Pi 4: power the Pi
up first, start HA several minutes later, and verify unattended boot. Repeat with
DHCP unavailable, an interrupted TFTP download, NFS starting after TFTP, and an
OS rollback. Check SD contents/checksums before and after repeated boots and
updates. Verify native Ethernet boot separately on capable boards.

The SD update tests use regular temporary files, never block devices. They build
real three-partition FAT images, interrupt inactive-slot writes, check recovery
and active partitions are unchanged, simulate trial failure/commit interruption,
verify signed download handling, and check unchanged firmware causes zero SD
writes. Physical `tryboot`, cold-start, early firmware hang and electrical
power-loss checks remain required on each supported board.
