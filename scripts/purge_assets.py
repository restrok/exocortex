#!/usr/bin/env python3
"""Standalone purge utility for Exocortex Assets Store ephemeral/tmp files.

Can be run via cron or systemd timer using standard Python 3 (no third-party deps).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ASSETS_ROOT = Path(os.getenv("EXOCORTEX_ASSETS_DIR", "/home/fsirio/assets"))


def purge_assets(
    root: Path,
    category: str | None = "tmp",
    dry_run: bool = False,
) -> list[dict[str, object]]:
    """Purge expired assets from SQLite index and filesystem."""
    db_path = root / ".meta" / "index.db"
    if not db_path.exists():
        print(f"[WARN] Database not found at {db_path}. Nothing to purge.")
        return []

    now_iso = datetime.now(UTC).isoformat()
    query = (
        "SELECT asset_id, path, filename, sha256, expires_at FROM assets "
        "WHERE retention = 'ttl' AND expires_at IS NOT NULL AND expires_at <= ?"
    )
    params: list[object] = [now_iso]
    if category:
        query += " AND path LIKE ?"
        params.append(f"{category}/%")

    purged: list[dict[str, object]] = []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            asset_id = row["asset_id"]
            rel_path = row["path"]
            file_path = root / rel_path
            info: dict[str, object] = {
                "asset_id": asset_id,
                "path": rel_path,
                "expires_at": row["expires_at"],
                "file_deleted": False,
            }

            if not dry_run:
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        info["file_deleted"] = True
                    except OSError as err:
                        info["error"] = str(err)
                conn.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))

            purged.append(info)

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return purged


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Purge expired assets from Exocortex assets store."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ASSETS_ROOT,
        help="Root directory of assets store (default: /home/fsirio/assets)",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="tmp",
        help="Target category to purge (default: tmp, use '' for all categories)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate purge without deleting files or DB entries",
    )
    args = parser.parse_args()

    category = args.category if args.category else None
    mode_str = "DRY RUN" if args.dry_run else "LIVE"
    cat_desc = category or "ALL"
    print(
        f"[{mode_str}] Scanning for expired assets in {args.root} "
        f"(category: {cat_desc})..."
    )

    results = purge_assets(args.root, category=category, dry_run=args.dry_run)
    print(f"Purged {len(results)} expired asset(s).")
    for item in results:
        if item.get("file_deleted"):
            status = "DELETED"
        elif args.dry_run:
            status = "WOULD DELETE"
        else:
            status = "MISSING FILE"
        print(
            f" - [{status}] {item['path']} "
            f"(id: {item['asset_id']}, expires_at: {item['expires_at']})"
        )


if __name__ == "__main__":
    main()
