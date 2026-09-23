## 0.4.0

- Reuse prepared bases by upstream image SHA256; unchanged APT plans skip rebuilds.
- Apply OS updates locally to private cached-base copies, using QEMU when needed.
- Writable per-client NFS OS roots replace the RAM overlay; APT changes survive reboot.
- Podman images/layers persist in a per-client ext4 file stored and accessed over NFS.
- Remove overlay_size/podman_size; add per-client container_storage_gib for initial disk capacity.

## 0.3.0

- SD format v2: immutable recovery files plus two firmware slots and one-shot trial boots.
- Automatically update firmware/device trees from the staged OS and U-Boot/scripts from the add-on.
- Verify downloads and read-back before reboot; commit after health confirmation, quarantine failed trials.
- Write only changed SD payloads; native clients never inspect or write SD cards.
- Earlier single-partition cards require a one-time reflash; no automatic repartitioning.

## 0.2.0

- Optional immutable SD retry loaders and image build scripts for Pi 1/2/3B/3B+/4.
- Separate 32-bit Raspberry Pi OS generation builds for Pi 1/2, including v6/v7 kernels.
- Atomic SD boot manifests, verified kernel/initramfs downloads and NFS mount retries.
- Native network boot retained alongside SD-assisted boot; no routine SD writes.
- Manual GitHub Actions SD image artifact builds and expanded boot protocol/image checks.

# 0.1.0

Initial implementation: ARM64 Pi network boot, immutable NFS OS generations,
RAM overlays, separate persistent application data, signed APT sources, Podman
Quadlets, automatic updates, boot health confirmation and generation rollback.
