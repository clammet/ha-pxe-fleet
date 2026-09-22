#!/usr/bin/env bash
set -euo pipefail
exec bash /source/build-tools.sh "${1:-all}"
