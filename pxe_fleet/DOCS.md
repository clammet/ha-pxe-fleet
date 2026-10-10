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
already booted NFS-mode clients still depend on their original NFS server address.

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

## Client storage mode

Set `os_storage` under each client **before the add-on first registers it**.
Registration stores the choice in the controller's durable state. Later changes
are ignored with a warning; they do not migrate data, rebuild a root or reboot a
client solely because the requested mode changed. Existing clients without the
setting remain `nfs`. Removing and re-adding the same serial also preserves its
original choice because client state is retained.

| Client setting | Default | Meaning |
| --- | --- | --- |
| `os_storage` | `nfs` | Creation-time choice: `nfs` or `ram` |
| `ram_root_size` | `50%` | RAM mode: maximum space for the complete OS and its changes |
| `ram_data_size` | `10%` | RAM mode: maximum space for application data |
| `ram_container_size` | `25%` | RAM mode: maximum space for Podman images and writable layers |
| `container_storage_gib` | 8 | NFS mode: initial Podman disk capacity in GiB |

RAM limits accept strings such as `512M`, `4G` or `50%` (1–99% of physical RAM).
They are maximum capacities, not reserved allocations. Leave memory for processes,
the kernel, initramfs and runtime files. Limits are independent: oversized totals
can exhaust physical RAM before a filesystem reaches its own limit. RAM size
settings can be adjusted through a new OS generation; the storage mode stays fixed.

**NFS mode:** Each client has a writable NFS OS root and separate writable appdata.
System files, libraries, APT databases and package updates persist across ordinary
reboots. A replacement generation replaces the root. Linux uses its normal
reclaimable file caches and writes dirty data back to the server; exports use
`sync` and clients use `hard` mounts. Applications still need their normal
fsync/transaction semantics. The client depends on NFS while running.

**RAM mode:** Initramfs mounts the generation read-only, copies the **entire** root
into tmpfs, then fully unmounts NFS before starting the OS. There is no NFS lower
layer or on-demand fetching of OS files. `/appdata` and `/var/lib/containers` are
separate tmpfs mounts; no NFS filesystem remains mounted. Podman downloads images
from the registry on each boot. Once applications have started, losing NFS or
the controller does not stop them. Application-specific network services may
still be required; registry/APT updates need their repositories, and the next
boot always needs the controller's boot files and OS export.

RAM application data starts fresh each boot. APT data directories are seeded from
the packaged defaults; container volumes start empty. The existing `apt.persistent`
and container `volumes` mappings still select isolated appdata directories, but
**they are volatile in RAM mode**. There is no checkpointing, write-through,
background synchronization or import from the server's appdata. Runtime APT
updates, downloaded container images, logs, appdata, registry login credentials
and SSH host keys are lost on reboot, including automatic update reboots.
The next boot loads the centrally built OS/apps and pulls configured containers.
Use this mode for stateless/reconstructible workloads.

The whole prepared OS must fit in `ram_root_size`, with additional room for APT
updates. Tmpfs contents consume RAM until deleted/unmounted; they cannot be
reclaimed like clean NFS page cache. No swap is configured. An incomplete copy
fails boot instead of falling back to NFS or starting a partial OS. Stock prepared
images are much too large for the smallest Pis; NFS mode is the practical choice
for Pi 1/2 and other boards without enough memory. Check the deployed root size
and actual workload before choosing RAM mode.

Small runtime files and the bounded 32 MiB volatile journal use RAM in both modes.
On the host, allow a prepared base, a private update copy, downloaded images and
retained per-client roots (roughly 20 GB of build/cache headroom per architecture,
plus NFS client storage). Backing files live under the add-on's `/data/fleet`.

The old global `overlay_size` and `podman_size` options remain unsupported. This
RAM mode is a complete RAM root, not the former read-only NFS plus RAM overlay.
SD format-v2 cards need no manual reflash to select the new boot behavior.

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
into appdata. They follow the OS storage mode: persistent on NFS, volatile in RAM.
Application logs should use the bounded journal unless durable logging is needed.
Configuration under `/etc` survives ordinary reboots only in NFS mode and is replaced with a new generation;
package it or have the application read it from its persistent directory.

The add-on installs configured APT applications on local host storage while
building a private OS generation, using QEMU for cross-architecture scripts.
Package-provided initial data is seeded into appdata only on first creation.
Existing appdata is never reseeded. The Pi starts these preinstalled versions
without downloading them again. At the configured interval it pulls application
updates with APT and installs them into its selected root. NFS mode retains them
across reboots; RAM mode loses them on reboot. Neither path merges live changes
into the host's cached base. The host preinstalls current repository packages
when it builds the next generation.

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

The add-on generates rootful Podman Quadlets. In **NFS mode**, each client with containers gets
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

**RAM mode** uses a dedicated tmpfs at `/var/lib/containers`, bounded by
`ram_container_size`. It creates no ext4 image on NFS. Images and writable layers
are downloaded/created anew on every boot, and appdata volumes are also volatile.
The client checks that appdata and container mounts match its configured mode
before starting applications.

For a private registry, log in on the Pi with:

```sh
podman login --authfile /appdata/.fleet/registry-auth.json registry.example.org
```

Quadlets and auto-update both use this auth file. It persists across OS resets
in NFS mode. RAM mode loses it on reboot, so private registries need a separate
credential provisioning mechanism before containers can start unattended.

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
   sources, service accounts and application packages offline. Export it read-write
   for an NFS client or read-only for a RAM client, and
   publish a complete immutable TFTP payload. An atomic `config.txt` selects the
   payload using `os_prefix`, including its matching kernel, modules and initramfs.
   SD loaders read an atomic `boot.env` pointing to the matching immutable boot payload,
   with a U-Boot-compatible kernel plus file sizes and SHA256 hashes. Changed SD
   firmware/device trees/U-Boot are staged in the inactive card slot before reboot;
   an SD trial cannot confirm the OS until the boot slot is committed. NFS root
   mounting retries indefinitely if exports are not ready yet. RAM clients copy
   this root and disconnect the NFS mount before starting the OS.
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

RAM clients keep their small SD update transaction journal on the controller
through signed HTTP requests with replay protection. This survives RAM resets
without an NFS mount or routine SD writes. If the controller cannot acknowledge
a journal change, the agent restarts and reloads it before continuing; running
applications are unaffected. SD updates/confirmation require controller contact.
NFS clients continue to keep this journal in their persistent appdata.

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

In NFS mode, OS rollback does not roll back appdata or APT/container registry
versions. In RAM mode, rollback reboots and loses all runtime appdata.
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
quarantines the current one; it requires no pending rollout. RAM mode resets all
volatile runtime state during either an update or a rollback. Requests are queued
and their result appears in the log. The status command omits client secrets.
In NFS mode, rebooting retains OS changes, installed application updates and
Podman images; OS replacement retains appdata, the container disk and SSH identity.
In RAM mode, every reboot loses all of those runtime changes and application data.

Restart the add-on to load edited configuration. Do not rename/delete exported
root directories, run host-side APT against a live root, or move storage behind
an NFS client. Normal client-side writes to an NFS-mode client's own root are expected. RAM-mode
roots are read-only exports and are copied into client memory at boot.

## Design references

* [Raspberry Pi network boot](https://www.raspberrypi.com/documentation/computers/remote-access.html)
* [Raspberry Pi boot prefixes](https://www.raspberrypi.com/documentation/computers/config_txt.html)
* [Raspberry Pi OS major upgrades](https://www.raspberrypi.com/documentation/computers/os.html)
* [Linux OverlayFS requirements](https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html)
* [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
* [Podman auto-update](https://docs.podman.io/en/latest/markdown/podman-auto-update.1.html)

* [Linux tmpfs memory and capacity semantics](https://www.kernel.org/doc/html/latest/filesystems/tmpfs.html)
