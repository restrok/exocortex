#!/usr/bin/env bash
# Wrapper to purge expired assets from tmp/ directory
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="${EXOCORTEX_ASSETS_DIR:-/home/fsirio/assets}"

python3 "${SCRIPT_DIR}/purge_assets.py" --root "${ASSETS_DIR}" --category "tmp" "$@"
