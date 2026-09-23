# Configuration and operation

## Supported systems and setup

The add-on runs on ARM64 or x86-64 Linux Home Assistant hosts and provisions
Pi 1/2 clients with 32-bit Raspberry Pi OS and Pi 3/4/5 with 64-bit OS, using
wired Ethernet. `pi3` covers 3B/3B+. Optional
[SD retry cards](../boot-media/README.md) support Pi 1B/B+, Pi 2B, Pi 3B/3B+
and Pi 4B and wait for HA to become available without routine card writes.
Format-v2 cards automatically update firmware/U-Boot in an inactive slot, verify
it, and use a one-shot trial boot before committing after health confirmation.
Earlier single-partition cards need a one-time reflash; they are never repartitioned
automatically. Set `sd_updates: false` on a client to disable card updates.

Native Ethernet boot remains available on capable boards: Pi 3B needs network
boot enabled in its OTP configuration; Pi 4/5 require an EEPROM boot order that
includes network boot. Pi 1 and early Pi 2 cannot boot directly from Ethernet.
For SD boot, configure a DHCP reservation and embed the HA server address in the
card; no DHCP PXE options are needed. Pi 3B+ uses `pi3plus` in the card builder
and `pi3` in `fleet.yaml`. Pi 5 currently supports only the native route.

Client `boot_options` control the native route's firmware settings. For SD boot,
supply peripheral options to the card builder with `--boot-option`; firmware
loads them before U-Boot. Subsequent automatic slot updates apply the client
`boot_options` using overlays from the target OS. Keep first-boot card options
and fleet configuration aligned. SD boot requires the reserved `gpu_mem=32` layout.

The public add-on configuration directory is exposed as `/config` inside the
add-on and usually as `addon_configs/<repository-id>_pxe_fleet` through Home
Assistant's file tools. Put `fleet.yaml` there. The only Supervisor option is
`config_file`, a filename relative to that directory. A complete example is in
[`examples/fleet.yaml`](../examples/fleet.yaml).

Configure DHCP reservations matching each client's `ip`, and use the last eight
hex digits of its hardware serial. Full 16-digit serials are accepted and
normalized; duplicate suffixes are rejected. The control endpoint also checks
the reserved client IP. Keep the server address fixed. Changing a server address
requires updating network boot configuration and rebooting clients manually;
already booted clients still depend on their original NFS server address.

Allow UDP 69 and TFTP's negotiated UDP transfers, TCP 2049 for NFSv4.1, and the
configured control TCP port (8099 by default). Do not run another NFS/TFTP server
on the same Home Assistant host. Use a trusted LAN or provisioning VLAN: TFTP
and NFS AUTH_SYS are not encrypted or authenticated against hostile LAN clients.
Signed control replies prevent unauthenticated reboot commands, but this is
not a secure-boot system. Root exports are restricted to each reserved address.

## Fleet settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `server_ip` | Required | HA host's fixed IPv4 LAN address |
| `dns` | Server address | DNS resolver addresses; normally your router |
| `os_check_hours` | 24 | Rebuild/check interval for OS updates |
| `app_update_minutes` | 60 | Client APT/Podman update interval |
| `boot_timeout_seconds` | 900 | Time after contact/reboot request to confirm health |
| `min_free_gib` | 8 | Stop staging before disk space becomes critical |
| `control_port` | 8099 | Signed client report/reboot endpoint |
| `ssh_authorized_keys` | `[]` | Root SSH public keys; SSH is disabled when empty |
| `image` | `{}` | Optional fixed ARM64 HTTPS image URL and SHA256 |
| `image_armhf` | `{}` | Optional fixed 32-bit Raspberry Pi OS image URL and SHA256 for Pi 1/2 |

Each client has a writable NFS OS root and a separate writable appdata export.
OS files and package databases persist across ordinary reboots; replacing the OS
generation replaces that root. There is no tmpfs root overlay. Linux uses normal
reclaimable file caches and writes dirty data back to the NFS server; the server
exports use `sync` and clients use `hard` mounts. This is not a special RAM cache
or a promise that every write syscall is synchronously durable: applications must
still use their normal fsync/transaction semantics.

Small runtime files and the bounded volatile journal remain in RAM. No swap is
configured. Pi 1 still needs ARMv6-compatible applications and sufficient process
memory. Allow space for a prepared base, a private update copy, downloaded images,
and retained per-client roots (roughly 20 GB of build/cache headroom per architecture,
plus client storage). All backing files live under the add-on's `/data/fleet`.

The old `overlay_size` and `podman_size` settings have been removed; remove them
from `fleet.yaml`. Existing deployments pick up the new mount layout when they
boot a newly built generation; SD format-v2 cards do not need reflashing for this.

## APT applications

Each client has an `apt` mapping:

* `sources`: optional third-party repositories with `name`, HTTPS `url`, `suites`,
  `components`, HTTPS `key_url` and `key_sha256`. Keys are checksum-verified and
  scoped to their source using `Signed-By`. `{codename}` expands to the booted
  OS's Debian codename. Stock OS repositories are inherited from the image.
* `packages`: package names to install and keep current, without versions or
  command-line options. Kernel/firmware updates belong to the central OS builder.
* `services`: explicit systemd `.service` names. Services start only after app
  installation and persistent mounts succeed. They restart when packages change.
* `users`: optional stable service accounts (`name`, numeric `uid` and `gid`,
  between 2000 and 60000), created before package installation. Use the account
  names expected by the packages, so upgrades cannot silently change ownership.
* `persistent`: directory bind mounts with an appdata-relative `source`, an
  application `target` under `/var/lib`, `/var/cache` or `/opt`, and numeric `uid`
  and `gid`. Existing data ownership must match; it is never recursively changed.

[`examples/apt-source.yaml`](../examples/apt-source.yaml) shows a custom source.
An application package should supply its own configuration and service unit.
Configure that application to write durable state into its persistent directory.
Keep package-manager databases in the disposable OS root; do not redirect them
into appdata. They are now disk-backed on NFS and survive reboots of that root.
Application logs should use the bounded journal unless durable logging is needed.
Configuration under `/etc` survives reboots but is replaced with a new generation;
package it or have the application read it from its persistent directory.

The add-on installs configured APT applications on local host storage while
building a private OS generation, using QEMU for cross-architecture scripts.
Package-provided initial data is seeded into appdata only on first creation.
Existing appdata is never reseeded. The Pi starts these preinstalled versions
without downloading them again. At the configured interval it pulls application
updates with APT and installs them into its writable NFS root. Those updates
survive reboot; they are not held in RAM or merged into the host's cached base.
This client-side update path trades NFS write speed for low RAM use.

OS/security updates run on the host against a private copy of its prepared base,
then produce a replacement client generation. Client kernel updates are pinned
out to keep the running kernel, modules and served boot payload consistent.
A replacement generation freshly installs the application's current repository
version. The host never edits an exported root or tries to copy a live dpkg
transaction from a client.

If a third-party repository has not published packages for a new major OS,
staging fails and the running OS remains in place. Automatic OS tracking does not make
third-party packages compatible with future releases.

## Podman applications

`containers` is a list of objects with a unique `name` and a fully qualified
registry `image`, such as `docker.io/library/nginx:stable-alpine`. Supported fields
are `environment` (string values), `command` (argument list), `ports`, `devices`,
`network` (`bridge` or `host`), and `volumes`. Each volume has an appdata-relative
`source`, container `target`, optional numeric `uid`/`gid` (default 0) and optional
`read_only` boolean. Use a mutable tag for automatic image updates; a digest
deliberately pins the image.

The add-on generates rootful Podman Quadlets. Each client with containers gets
one sparse ext4 disk-image file at `/appdata/.fleet/podman.ext4`, created and
formatted once on the add-on host. The Pi mounts that file through NFS with a
loop device at `/var/lib/containers`. Podman's OverlayFS storage therefore sees
ext4, including the extended attributes it requires; all backing bytes still
travel over NFS. There is no client USB/SSD, RAM image store, or NBD/iSCSI service.

Set `container_storage_gib` on the client to choose the initial virtual capacity
(default 8 GiB). This setting applies when creating the file; existing disks are
never reformatted or resized automatically. Sparse allocation grows as used, so
monitor actual server space and free space inside the container filesystem.
Images and writable layers persist across both reboots and OS replacement.
The client refuses to start applications if the expected ext4 mount is absent
or backed by a different file. Only one Pi may mount its assigned image.

Do not mount or modify a client's ext4 image from the host while it is in use.
Back up the image only with that client's containers/filesystem quiesced; this is
a real filesystem, so interruption or exhausted server storage can require an
offline filesystem check. Boot uses ext4's normal journal recovery; never repair
a mounted image. These requirements also apply after a network/server outage.
Application volumes remain separate directories on appdata; use them for important
data rather than relying on a container's writable layer. Logs use the bounded
volatile journal. Podman auto-update restarts changed containers and requests its
built-in rollback if restart fails.
The container disk is not rolled back with the OS; a Podman storage database
upgrade can prevent an older Podman version from using it after an OS rollback.

For a private registry, log in on the Pi with:

```sh
podman login --authfile /appdata/.fleet/registry-auth.json registry.example.org
```

The auth file persists across OS resets; Quadlets and auto-update both use it.

## Update and recovery behavior

1. Resolve the official Lite image URL and checksum for each architecture.
   Persist that SHA256 with the prepared base. Download/extract/build from the
   upstream image only on first use or a changed checksum. This deliberately uses
   hash gating, not unreliable major-version inference from filenames. A new
   image within the same Debian release can still trigger a fresh build. Pinning
   `image` or `image_armhf` fixes the upstream image but still permits APT updates.
2. For an unchanged hash, reuse the already-updated, unexported base. Refresh APT
   metadata and simulate the upgrade/prerequisite installation. With no package
   or builder changes, skip copying the base, installing packages and rebuilding
   initramfs. New packages are applied locally to a private copy, with QEMU as
   needed. A failed update leaves the previously committed base selected.
3. Fingerprint the image hash, installed base packages, builder and client
   configuration. An unchanged result creates no generation or reboot. The
   cache is never a client's root and is never exported. Updating one architecture
   does not change the cache or existing roots for another architecture.
4. Copy the prepared OS into a new per-client generation and install its APT
   sources, service accounts and application packages offline. Export it read-write to that Pi and
   publish a complete immutable TFTP payload. An atomic `config.txt` selects the
   payload using `os_prefix`, including its matching kernel, modules and initramfs.
   SD loaders read an atomic `boot.env` pointing to the matching immutable boot payload,
   with a U-Boot-compatible kernel plus file sizes and SHA256 hashes. Changed SD
   firmware/device trees/U-Boot are staged in the inactive card slot before reboot;
   an SD trial cannot confirm the OS until the boot slot is committed. NFS root
   mounting retries indefinitely if exports are not ready yet.
5. The running client receives a signed reboot request. It finishes any running
   application update first. After reboot, mounts and apps must be ready and the
   declared services active for at least 60 seconds before confirmation.
6. If confirmation times out, select the last confirmed generation and quarantine
   the failed candidate. A still-running agent reboots into the fallback. A board
   stuck before the agent starts may need a power cycle; changing a boot target
   alone cannot reset hung hardware. Kernel panic and systemd watchdog policies
   cover some, but not all, failure modes. SD firmware-only trials have the same
   `boot_timeout_seconds` deadline even without an OS rollout. A failed trial
   retains the old selector and is quarantined; the retry command below also
   releases SD quarantine. See the SD guide for recovery-layer and EEPROM limits.

Offline clients do not start the rollout timer until they report. On first
deployment there is no known-good generation; failed preparation retries without
an automatic reboot loop. Health means systemd services are active, not that an
application-specific API or database migration is correct. Add application-level
readiness to the application's own service where required.

The active, previous, pending and last-reported roots are retained. Other roots
are reclaimed only after seven days without a reference. A stale report from an
offline Pi keeps its root alive. Appdata is never garbage-collected. Removing a
client from the configuration stops new deployments but retains its state, exports
and data so an existing NFS-root client is not broken unexpectedly. Decommission
the Pi before manually deleting retained client data. Low disk space blocks new
builds and is reported in the log; running clients retain their current roots.

OS rollback does not roll back appdata or APT/container registry versions.
Database migrations can make old applications incompatible with newer data.
Back up application data using application-consistent backups, and coordinate
schema compatibility with application updates. HA backups of the add-on include
its large image cache and generations as well as data; plan backup capacity.

## Status and manual controls

The add-on log reports builds, activation, confirmation and rollback. On a Pi,
inspect `journalctl -u fleet-agent -u fleet-prepare` and
`systemctl status fleet-<container-name>.service`.

From a shell inside the add-on container:

```sh
python3 -m fleet.ctl status
python3 -m fleet.ctl check
python3 -m fleet.ctl retry 1234abcd
python3 -m fleet.ctl rollback 1234abcd
```

`check` schedules an immediate OS build/check. `retry` also clears quarantined
builds for one client. `rollback` requests the previous confirmed generation and
quarantines the current one; it requires no pending rollout. Requests are queued
and their result appears in the log. The status command omits client secrets.
Rebooting a Pi retains its NFS OS changes, installed application updates and
Podman images. An OS replacement or rollback selects a different root; appdata,
the container disk and SSH host identity remain intact.

Restart the add-on to load edited configuration. Do not rename/delete exported
root directories, run host-side APT against a live root, or move storage behind
an NFS client. Normal client-side writes to that client's own root are expected.

## Design references

* [Raspberry Pi network boot](https://www.raspberrypi.com/documentation/computers/remote-access.html)
* [Raspberry Pi boot prefixes](https://www.raspberrypi.com/documentation/computers/config_txt.html)
* [Raspberry Pi OS major upgrades](https://www.raspberrypi.com/documentation/computers/os.html)
* [Linux OverlayFS requirements](https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html)
* [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
* [Podman auto-update](https://docs.podman.io/en/latest/markdown/podman-auto-update.1.html)
