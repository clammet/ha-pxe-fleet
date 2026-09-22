# Configuration and operation

## Supported systems and setup

The add-on runs on ARM64 or x86-64 Linux Home Assistant hosts and provisions
ARM64 Pi 3, Pi 4 and Pi 5 clients with wired Ethernet. `pi3` covers 3B/3B+;
3B needs network boot enabled in its OTP configuration. Pi 4/5 require an EEPROM
boot order that includes network boot. Other boards and SD-assisted U-Boot boot
are outside this initial implementation.

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
| `overlay_size` | `50%` | Maximum RAM filesystem size for OS changes |
| `podman_size` | `25%` | Maximum RAM filesystem size for container storage |
| `control_port` | 8099 | Signed client report/reboot endpoint |
| `ssh_authorized_keys` | `[]` | Root SSH public keys; SSH is disabled when empty |
| `image` | `{}` | Optional fixed HTTPS image URL and SHA256 |

The two RAM limits are independent ceilings, not reservations. Processes still
need memory. A Pi 3 with 1 GB RAM is suitable only for small packages and images;
large installations can exhaust RAM. No swap is configured. Start with one Pi
and measure the workload before deploying a fleet. Allow roughly 15 GB for build
workspace/cache, plus several GB per retained client generation. All server data
lives in the add-on's `/data/fleet` on local storage, never on a remote NFS mount.

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
Package-manager databases, all of `/var`, and Podman graph storage must not be
made persistent. Application logs should go to the journal or a RAM directory
unless persistence is needed. Configuration under `/etc` is disposable; package
it or have the application read it from its persistent directory.

The add-on installs configured APT applications while building the private OS
generation. Package-provided initial data is copied into a persistent directory
only when that directory is first created. Existing appdata is never reseeded.
The Pi can start those staged application versions without downloading packages
again. On each boot and then at the configured interval it pulls and installs
updates with APT in its RAM overlay, avoiding the many small NFS writes from
dpkg. Reboot discards those RAM updates, so APT reapplies any changes newer than
the staged package versions. OS updates happen separately in a fresh server
build, including security updates to the base system. Client kernel updates are
pinned out so running kernels and served boot files cannot drift apart.

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

The add-on generates rootful Podman Quadlets. Podman image layers and container
writable layers use a separate tmpfs, so each reboot pulls images again.
Persistent appdata is bind-mounted into containers. Do not put Podman's graphroot
on NFS: its overlay storage needs local filesystem semantics. Container logs use
the volatile system journal. Podman's `auto-update` restarts updated containers
and requests its built-in rollback when a service restart fails.

For a private registry, log in on the Pi with:

```sh
podman login --authfile /appdata/.fleet/registry-auth.json registry.example.org
```

The auth file persists across OS resets; Quadlets and auto-update both use it.

## Update and recovery behavior

1. Discover the current official ARM64 Lite release through Raspberry Pi's latest
   URL and verify its matching SHA256. A pinned `image: {url, sha256}` disables
   release discovery while keeping APT OS updates enabled.
2. Extract a clean image into private local storage. Run OS APT upgrades offline,
   install client prerequisites, and build initramfs images for both Pi kernel
   families. On x86-64, ARM64 maintainer scripts run through QEMU binfmt.
3. Fingerprint the upstream image, base package versions, builder and client
   configuration. An unchanged result causes no reboot or generation creation.
4. Copy the prepared OS into a new per-client generation and install its APT
   sources, service accounts and application packages offline. Export it read-only and
   publish a complete immutable TFTP payload. An atomic `config.txt` selects the
   payload using `os_prefix`, including its matching kernel, modules and initramfs.
5. The running client receives a signed reboot request. It finishes any running
   application update first. After reboot, mounts and apps must be ready and the
   declared services active for at least 60 seconds before confirmation.
6. If confirmation times out, select the last confirmed generation and quarantine
   the failed candidate. A still-running agent reboots into the fallback. A board
   stuck before the agent starts may need a power cycle; changing a boot target
   alone cannot reset hung hardware. Kernel panic and systemd watchdog policies
   cover some, but not all, failure modes.

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
Rebooting a Pi resets its RAM OS changes and starts its staged applications;
its appdata and SSH host identity remain intact.

Restart the add-on to load edited configuration. Do not rename/delete exported
root directories, modify a live root, or move storage behind an NFS client.

## Design references

* [Raspberry Pi network boot](https://www.raspberrypi.com/documentation/computers/remote-access.html)
* [Raspberry Pi boot prefixes](https://www.raspberrypi.com/documentation/computers/config_txt.html)
* [Raspberry Pi OS major upgrades](https://www.raspberrypi.com/documentation/computers/os.html)
* [Linux OverlayFS requirements](https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html)
* [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
* [Podman auto-update](https://docs.podman.io/en/latest/markdown/podman-auto-update.1.html)
