#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/work" \
  --mount "type=bind,src=$PWD/../pxe_fleet,dst=/pxe_fleet,readonly" \
  ha-pxe-fleet-sd-builder:local python3 scripts/prepare.py "$@"
