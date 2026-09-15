#!/usr/bin/env bash
# ==============================================================================
# Bootstrap Script for Exocortex Orchestrator Assets Store
#
# Idempotently creates directory layout, permissions, SQLite index, and ledger.
# Target: /home/fsirio/assets (or custom path via $EXOCORTEX_ASSETS_DIR or --root)
# Permissions: 750 for directories, 640 for metadata files, owner fsirio
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ASSETS_ROOT="${EXOCORTEX_ASSETS_DIR:-/home/fsirio/assets}"

# Parse optional --root flag
while [[ $# -gt 0 ]]; do
    case "$1" in
        --root|-r)
            ASSETS_ROOT="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--root <path>]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [--root <path>]" >&2
            exit 1
            ;;
    esac
done

echo "==> Initializing Assets Store at: ${ASSETS_ROOT}"

# Required subdirectories
CATEGORIES=("inbox" "generated" "media" "docs" "tmp" ".meta")

# 1. Create root and subdirectories with mode 750
mkdir -p "${ASSETS_ROOT}"
chmod 750 "${ASSETS_ROOT}"

for cat in "${CATEGORIES[@]}"; do
    target="${ASSETS_ROOT}/${cat}"
    if [ ! -d "${target}" ]; then
        echo "Creating directory: ${target}"
        mkdir -p "${target}"
    fi
    chmod 750 "${target}"
done

META_DIR="${ASSETS_ROOT}/.meta"
MANIFEST_PATH="${META_DIR}/manifest.jsonl"
INDEX_DB_PATH="${META_DIR}/index.db"
SCHEMA_SQL="${REPO_DIR}/docs/assets/schema.sql"

# 2. Initialize append-only manifest ledger if not present
if [ ! -f "${MANIFEST_PATH}" ]; then
    echo "Creating manifest ledger: ${MANIFEST_PATH}"
    touch "${MANIFEST_PATH}"
fi
chmod 640 "${MANIFEST_PATH}"

# 3. Initialize SQLite index.db using schema.sql via Python 3
echo "Initializing SQLite database schema at: ${INDEX_DB_PATH}"
python3 - <<EOF
import sqlite3
import sys
from pathlib import Path

db_path = Path("${INDEX_DB_PATH}")
schema_path = Path("${SCHEMA_SQL}")

if not schema_path.is_file():
    print(f"Error: Schema SQL file not found at {schema_path}", file=sys.stderr)
    sys.exit(1)

schema_ddl = schema_path.read_text(encoding="utf-8")
with sqlite3.connect(str(db_path)) as conn:
    conn.executescript(schema_ddl)
    conn.commit()
print("SQLite schema verified successfully.")
EOF

chmod 640 "${INDEX_DB_PATH}"

# 4. Copy / deploy purge utilities into .meta/
cp -p "${SCRIPT_DIR}/purge_assets.py" "${META_DIR}/purge_assets.py"
chmod 750 "${META_DIR}/purge_assets.py"

cat > "${META_DIR}/purge_tmp.sh" << 'PURGE_EOF'
#!/usr/bin/env bash
set -euo pipefail
META_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="$(cd "${META_DIR}/.." && pwd)"
python3 "${META_DIR}/purge_assets.py" --root "${ASSETS_DIR}" --category "tmp" "$@"
PURGE_EOF
chmod 750 "${META_DIR}/purge_tmp.sh"

# 5. Fix ownership if running as root or current user
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_GROUP="$(id -gn "${TARGET_USER}" 2>/dev/null || echo "${TARGET_USER}")"

if [ "$(id -u)" -eq 0 ] && [ "${TARGET_USER}" != "root" ]; then
    echo "Setting ownership to ${TARGET_USER}:${TARGET_GROUP}..."
    chown -R "${TARGET_USER}:${TARGET_GROUP}" "${ASSETS_ROOT}"
fi

echo "==> Assets store successfully initialized at ${ASSETS_ROOT}:"
ls -ld "${ASSETS_ROOT}"
ls -ld "${ASSETS_ROOT}"/* "${META_DIR}"/*
echo "==> Bootstrap completed."
