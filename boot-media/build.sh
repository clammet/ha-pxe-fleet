#!/usr/bin/env bash
# Build native Linux tools through Docker on Linux or macOS. No privileged mounts.
set -euo pipefail
cd -- "$(dirname -- "$0")"
case "${1:-all}" in
  all|pi1|pi2|pi3|pi3plus|pi4) model="${1:-all}" ;;
  *) echo "Usage: $0 [all|pi1|pi2|pi3|pi3plus|pi4]" >&2; exit 2 ;;
esac
mkdir -p .cache out
docker build -t ha-pxe-fleet-sd-builder:local .
docker run --rm --user "$(id -u):$(id -g)" -e JOBS="${JOBS:-4}" \
  --mount "type=bind,src=$PWD,dst=/work" \
  --mount "type=bind,src=$PWD/../pxe_fleet/bootloader,dst=/source,readonly" \
  ha-pxe-fleet-sd-builder:local bash scripts/build-tools.sh "$model"
