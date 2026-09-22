# Optional SD-assisted network boot

The card contains Raspberry Pi firmware, U-Boot and a small retry script. It
contains **no deployed OS, application or app data**. On each boot it fetches a
complete kernel/initramfs generation from the add-on, verifies file sizes and
SHA256 hashes, and starts the usual NFS root with its RAM overlay and separate
persistent `/appdata` mount.

If HA is still starting, DHCP fails, or a download is interrupted, the loader
retries indefinitely. If TFTP becomes ready before NFS does, the initramfs keeps
retrying the NFS mount. Normal boots do not write SD. When the firmware, device
trees, U-Boot or retry script changes, the agent automatically stages a new boot
slot and tests it with Raspberry Pi's one-shot `tryboot` mode. U-Boot's
environment remains in RAM; there is no persistent boot counter or saved environment.

Native Ethernet boot remains available independently. Both routes follow the
same generation selection, health confirmation and rollback. No `boot_mode`
setting is needed in `fleet.yaml`.

## Boards

| Hardware | SD builder model | Add-on `model` | Deployed OS |
| --- | --- | --- | --- |
| Pi 1 Model B / B+ (onboard Ethernet) | `pi1` | `pi1` | 32-bit ARMv6 |
| Pi 2 Model B (including later revisions) | `pi2` | `pi2` | 32-bit ARMv7 |
| Pi 3 Model B | `pi3` | `pi3` | 64-bit ARM |
| Pi 3 Model B+ | `pi3plus` | `pi3` | 64-bit ARM |
| Pi 4 Model B | `pi4` | `pi4` | 64-bit ARM |

Pi 1 and early Pi 2 boards need SD assistance; they cannot boot directly from
Ethernet. Pi 2 revisions with a BCM2837 and Pi 3B need their supported network
boot mode enabled for native boot. Pi 3B+ and Pi 4 can also use native network
boot with the appropriate boot configuration. See the
[Raspberry Pi boot documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#network-booting).
Pi 5 retains the existing native boot route; this SD loader does not support it.
Wi-Fi, Pi A variants, Compute Modules and external USB Ethernet adapters are not
covered by this initial implementation.

Pi 1 has very little RAM. Prefer small APT services; a 256 MB board can be
particularly constrained during initramfs unpacking and APT updates. Applications
and OCI images must support the actual CPU: Pi 1 needs ARMv6, not an ARMv7-only
`armhf` binary. Podman cannot make an incompatible image run. No swap writes to
SD are enabled. All boards still require physical boot validation; successful
builds alone do not establish hardware compatibility.

## Build a card

Install Docker with a Linux container engine (Docker Desktop or Colima on macOS).
Build commands run without privileged mode, loop devices or access to a physical
SD card. Run from the repository root:

```sh
# Compile once for your board, or use "all" for all five variants.
./boot-media/build.sh pi4

# Replace these example values with your Pi's serial and HA server address.
./boot-media/prepare.sh --model pi4 --serial 1234abcd --server 192.168.1.10
```

Read the serial on the Pi with `awk '/Serial/ {print $3}' /proc/cpuinfo`.
The full 16-digit serial or its last eight hexadecimal digits are accepted,
matching the add-on's normalization. Each card is bound to that serial; moving
it to another board produces a diagnostic and retries instead of booting another
client's filesystem.

The result is `boot-media/out/1234abcd-pi4/boot.img`, a 256 MiB MBR disk image with
three 64 MiB FAT32 partitions: recovery/selector, slot A and slot B. Both slots
initially contain the same loader. The remainder is unused. **Earlier
single-partition cards need a one-time reflash with this format.** The agent
never repartitions a deployed card. The directory also contains:

- `boot.img.sha256`: checksum of the complete image.
- `sd/`: initial files shared by the boot slots (card identity/selector are added to recovery).
- `boot.cmd`: readable loader source, corresponding to `sd/boot.scr`.
- `metadata.json`: firmware commit, loader version, addresses and file hashes.
- `uboot.config`, `COPYING`, `UBOOT-LICENSING`: build settings and licensing.

Check the checksum from that output directory using `sha256sum -c boot.img.sha256`
(or `shasum -a 256 -c boot.img.sha256` on macOS), then use Raspberry Pi Imager's
**Use custom** option to flash `boot.img`. Disable Imager OS customizations.
Flashing replaces the selected card's contents; the scripts themselves never
open a disk device. Image preparation refuses to overwrite an existing output
directory; move it aside when deliberately preparing a replacement.

Keep the Ethernet DHCP reservation from `fleet.yaml`. SD boot uses normal DHCP
for the client address and the embedded `--server` value for TFTP; it does not
need DHCP boot-server options or a PXE ProxyDHCP server. Native boot still needs
the DHCP/bootloader setup described in the add-on guide. On Pi 4, ensure EEPROM
boot order tries SD so that the inserted card is used.

## Automatic boot-module updates

A new kernel or OS does **not by itself** trigger an SD write: the kernel and
initramfs are downloaded on every boot. For every OS generation the add-on also
builds a firmware slot using that OS's firmware/device trees, the add-on's pinned
U-Boot binary and the current retry script. It compares a revision derived from
the boot files' contents, excluding OS generation IDs and timestamps. Unchanged
boot files cause no card writes, including after application-only updates.

Updates are automatic by default on format-v2 cards:

1. The signed controller reply identifies the firmware package for the target OS.
   The agent authenticates its download and checks compressed and expanded SHA256
   hashes and size limits. Downloads use RAM, not appdata or the SD card.
2. It identifies the booted SD card using firmware-reported partition information,
   hardware serial, a per-card ID and the exact partition layout. It refuses to
   write mounted/in-use cards or unrecognised layouts. Native network boots never
   look for or write an SD card, even if one happens to be inserted.
3. It writes only the inactive 64 MiB slot and reads every byte back for
   verification. The active slot and recovery files remain untouched.
4. It requests a one-shot `tryboot` reboot, keeping the previous slot as the
   normal boot selection. The new slot boots the target OS via the existing
   network manifest. The SD trial must pass before the OS rollout is confirmed.
5. After applications remain healthy for 60 seconds and the controller still
   selects that OS, the agent commits the slot. This changes one preallocated
   512-byte `autoboot.txt` data sector, without allocating files or modifying FAT
   directory entries. No routine filesystem mounts or SD writes are needed.

A changed boot payload normally costs one 64 MiB slot write and one 512-byte
selector commit. Update state lives in the protected `.fleet` directory on
appdata and is written only during state transitions. A firmware-only trial also
has its own `boot_timeout_seconds` deadline; it does not depend on an OS rollout
being in progress. Failed trials are quarantined to avoid repeated writes and
reboot loops. The existing `python3 -m fleet.ctl retry SERIAL` command clears that quarantine
as well as a failed OS generation. A new boot payload revision can be tried
automatically. See the add-on guide for the complete command.

The firmware clears the one-shot flag before trying the new slot, so a subsequent
reset selects the previous slot. Kernel panic and systemd watchdog policies cover
some failures. A board hanging before those mechanisms are running can still need
a power cycle. Logical interrupted-write tests do not prove electrical power-loss
safety or hardware compatibility; validate `tryboot` on each board before a fleet
rollout. Raspberry Pi 4B revisions 1.0/1.1 need a writable EEPROM for `tryboot`;
see the [official tryboot documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#fail-safe-os-updates-tryboot).

The recovery partition's firmware, ROM-facing `bootcode.bin`, identity and loader
are deliberately never rewritten automatically. Its selector is the only mutable
sector. A torn selector cannot be promoted by the updater; use the recovery path
and reflash/repair the card if needed. Rare changes to the ROM-facing recovery
layer, a new hardware model, or moving the card to a different Pi require a
manual reflash. This updater does not update the Pi's separate EEPROM.

To disable automatic SD updates for a client, set `sd_updates: false` in its
`fleet.yaml` entry. Native boot does not require this setting. Disabling updates
leaves firmware compatibility management to you. The server address should stay
reserved/static; changing it still requires rebuilding the recovery card because
that address is needed before the client can contact the controller.

U-Boot is compiled into each add-on image from a pinned, checksum-verified source
archive. U-Boot updates arrive through an add-on release that changes that pin;
the updater does not follow arbitrary upstream development builds. Firmware and
device tree updates follow the verified Raspberry Pi OS image and its signed APT
updates. Initial cards use a pinned firmware commit, overridable with
`--firmware-revision FULL_40_DIGIT_COMMIT`. Record the generated metadata alongside
images you distribute. Concurrent builds/preparations sharing `out` and `.cache`
are not supported.

For peripheral options needed on the first boot, pass them when making the card:

```sh
./boot-media/prepare.sh --model pi3plus --serial 1234abcd --server 192.168.1.10 \
  --boot-option dtparam=i2c_arm=on --boot-option dtoverlay=i2c-rtc,ds3231
```

Keep these in the client's `boot_options` too. Subsequent slot updates use those
options and matching overlays from the target OS; they therefore apply to both
native and SD boot. SD slots reserve a fixed memory layout and use `gpu_mem=32`.
The firmware applies overlays before U-Boot, which passes that prepared device
tree to Linux.

TFTP and NFS still assume a trusted, isolated LAN. SD update metadata and downloads
are authenticated with the client's controller key, but this does not turn the
whole system into signed secure boot. The card contains no control token or
application secrets.

## CI and troubleshooting

Push/PR CI checks the protocol, FAT image construction, interrupted SD writes, trial/rollback state and shell syntax. The
manual **Build SD boot media** GitHub Actions workflow cross-compiles every loader
and produces a downloadable ZIP artifact for each model. Provide the serial and
server address when dispatching it; these become visible workflow inputs and
artifact metadata. Artifacts are retained for 14 days and are not published as
container images or GitHub releases.

HDMI and 115200-baud UART show retries. Check the Pi serial, DHCP reservation,
Ethernet link, and HA's TFTP UDP 69/NFS TCP 2049 availability. The add-on must
have completed a generation before `<serial>/boot.env` is available. A model
mismatch or hash mismatch is rejected and retried. To return to native boot on
a capable board, remove the SD card and configure its normal Ethernet boot path.
