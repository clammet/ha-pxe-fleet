#!/usr/bin/env bash
set -euo pipefail
cd -- "${FLEET_BOOT_WORK:-/work}"
case "${1:-all}" in
  all) models=(pi1 pi2 pi3 pi3plus pi4) ;;
  pi1|pi2|pi3|pi3plus|pi4) models=("$1") ;;
  *) echo 'Unknown board' >&2; exit 2 ;;
esac
mkdir -p .cache out/bin
archive=.cache/u-boot-2026.07.tar.bz2
if [[ ! -f $archive ]]; then
  curl -fL --retry 3 --connect-timeout 20 --max-time 600 \
    https://ftp.denx.de/pub/u-boot/u-boot-2026.07.tar.bz2 -o "$archive.partial"
  mv "$archive.partial" "$archive"
fi
echo "78e8bfc382fe388f9b55aa1daf8c563522a037779b5d4c349d1415e381f1243e  $archive" | sha256sum --check
build_root=$(mktemp -d)
trap 'rm -rf "$build_root"' EXIT
tar -xjf "$archive" -C "$build_root"
source_tree="$build_root/u-boot-2026.07"
export SOURCE_DATE_EPOCH=1783382400 KBUILD_BUILD_USER=pxe-fleet KBUILD_BUILD_HOST=builder
for model in "${models[@]}"; do
  case "$model" in
    pi1) config=rpi_defconfig; cross=arm-linux-gnueabihf- ;;
    pi2) config=rpi_2_defconfig; cross=arm-linux-gnueabihf- ;;
    pi3) config=rpi_3_defconfig; cross=aarch64-linux-gnu- ;;
    pi3plus) config=rpi_3_b_plus_defconfig; cross=aarch64-linux-gnu- ;;
    pi4) config=rpi_4_defconfig; cross=aarch64-linux-gnu- ;;
  esac
  dest="$PWD/out/bin/$model"
  mkdir -p "$dest"
  ub="$build_root/$model"
  make -s -C "$source_tree" O="$ub" CROSS_COMPILE="$cross" "$config"
  # All environment and boot state stay in RAM. Do not run distro/EFI scans,
  # which could discover an unrelated OS or persistent boot state on a card.
  # U-Boot expands these variables at boot time, not in the build shell.
  # shellcheck disable=SC2016
  "$source_tree/scripts/config" --file "$ub/.config" \
    -d ENV_IS_IN_FAT -d ENV_IS_IN_MMC -e ENV_IS_NOWHERE -d BOOTCOUNT_LIMIT \
    -d USE_PREBOOT -d BOOTSTD_DEFAULTS -d EFI_LOADER -d NET_LWIP \
    -e USE_BOOTCOMMAND --set-str BOOTCOMMAND \
    'while true; do setenv fleet_part 1; if fdt addr ${fdt_addr}; then fdt get value fleet_part /chosen/bootloader partition; fi; if fatload mmc 0:${fleet_part} ${scriptaddr} boot.scr; then source ${scriptaddr}; fi; sleep 10; done' \
    --set-val BOOTDELAY -2 -e LEGACY_IMAGE_FORMAT -e CMD_SOURCE \
    -e CMD_HASH -e HASH -e SHA256 -e CMD_IMPORTENV -e CMD_MEMORY \
    -e CMD_SETEXPR -e CMD_SLEEP -e CMD_ITEST -e CMD_FDT -e CMD_FAT \
    -e CMD_DHCP -e CMD_TFTPBOOT -e NET -e LMB
  make -s -C "$source_tree" O="$ub" CROSS_COMPILE="$cross" olddefconfig
  for flag in ENV_IS_NOWHERE CMD_SOURCE CMD_HASH CMD_IMPORTENV CMD_ITEST CMD_DHCP CMD_TFTPBOOT CMD_FAT LMB; do
    grep -qx "CONFIG_${flag}=y" "$ub/.config" || { echo "Missing U-Boot feature: $flag" >&2; exit 1; }
  done
  make -s -C "$source_tree" O="$ub" CROSS_COMPILE="$cross" -j"${JOBS:-4}" > "$dest/build.log" 2>&1 || {
    tail -60 "$dest/build.log"; exit 1;
  }
  cp "$ub/u-boot.bin" "$dest/"
  cp "$ub/.config" "$dest/uboot.config"
  cp "$source_tree/Licenses/README" "$dest/UBOOT-LICENSING"
  cp "$source_tree/Licenses/gpl-2.0.txt" "$dest/COPYING"
  (cd "$dest" && sha256sum u-boot.bin uboot.config > SHA256SUMS)
  echo "Built $model loader in out/bin/$model"
done
