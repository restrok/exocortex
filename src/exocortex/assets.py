"""Orchestrator asset store manager and indexing engine."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

DEFAULT_ASSETS_ROOT = Path("/home/fsirio/assets")

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin TEXT,
    source_url TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    brain_note_id TEXT,
    retention TEXT NOT NULL DEFAULT 'keep',
    expires_at TEXT,
    visibility TEXT NOT NULL DEFAULT 'private'
);

CREATE INDEX IF NOT EXISTS idx_assets_sha256 ON assets(sha256);
CREATE INDEX IF NOT EXISTS idx_assets_origin ON assets(origin);
CREATE INDEX IF NOT EXISTS idx_assets_path ON assets(path);
CREATE INDEX IF NOT EXISTS idx_assets_created_at ON assets(created_at);
CREATE INDEX IF NOT EXISTS idx_assets_retention_expires
    ON assets(retention, expires_at);
CREATE INDEX IF NOT EXISTS idx_assets_visibility ON assets(visibility);
CREATE INDEX IF NOT EXISTS idx_assets_brain_note ON assets(brain_note_id);
"""

RetentionType = Literal["keep", "ttl"]
VisibilityType = Literal["private", "shareable"]
CategoryType = Literal["inbox", "generated", "media", "docs", "tmp", ".meta"]


class AssetRecord(BaseModel):
    """Metadata representation of an indexed asset."""

    asset_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    path: str
    filename: str
    mime_type: str | None = None
    size_bytes: int
    sha256: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    origin: str | None = None
    source_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    brain_note_id: str | None = None
    retention: RetentionType = "keep"
    expires_at: str | None = None
    visibility: VisibilityType = "private"


def compute_sha256(file_path: Path, chunk_size: int = 65536) -> str:
    """Calculate SHA256 hex digest for a file."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def detect_mime_type(file_path: Path) -> str:
    """Detect MIME type from file extension with fallback."""
    mime, _ = mimetypes.guess_type(str(file_path))
    if mime:
        return mime
    suffix = file_path.suffix.lower()
    fallback_map = {
        ".md": "text/markdown",
        ".jsonl": "application/x-jsonlines",
        ".log": "text/plain",
        ".webp": "image/webp",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".parquet": "application/vnd.apache.parquet",
    }
    return fallback_map.get(suffix, "application/octet-stream")


class AssetStore:
    """Manages filesystem storage, append-only manifest, and SQLite index for assets."""

    CATEGORIES: tuple[str, ...] = (
        "inbox",
        "generated",
        "media",
        "docs",
        "tmp",
        ".meta",
    )

    def __init__(self, root: Path | str | None = None) -> None:
        """Initialize asset store with configured or default root directory."""
        if root is not None:
            self.root = Path(root).expanduser().resolve()
        else:
            env_path = os.getenv("EXOCORTEX_ASSETS_DIR")
            self.root = (
                Path(env_path).expanduser().resolve()
                if env_path
                else DEFAULT_ASSETS_ROOT
            )
        self.meta_dir = self.root / ".meta"
        self.manifest_path = self.meta_dir / "manifest.jsonl"
        self.db_path = self.meta_dir / "index.db"

    def ensure_structure(self, dir_mode: int = 0o750, file_mode: int = 0o640) -> None:
        """Create assets directory tree, manifest file, and index DB idempotently."""
        self.root.mkdir(mode=dir_mode, parents=True, exist_ok=True)
        try:
            os.chmod(self.root, dir_mode)
        except OSError:
            pass

        for category in self.CATEGORIES:
            cat_dir = self.root / category
            cat_dir.mkdir(mode=dir_mode, parents=True, exist_ok=True)
            try:
                os.chmod(cat_dir, dir_mode)
            except OSError:
                pass

        if not self.manifest_path.exists():
            self.manifest_path.touch(mode=file_mode, exist_ok=True)
            try:
                os.chmod(self.manifest_path, file_mode)
            except OSError:
                pass

        self._init_db(file_mode=file_mode)

    def _get_db_connection(self) -> sqlite3.Connection:
        """Open a SQLite connection with row factory enabled."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self, file_mode: int = 0o640) -> None:
        """Initialize SQLite database with assets table and indexes."""
        with self._get_db_connection() as conn:
            conn.executescript(SCHEMA_DDL)
            conn.commit()
        if self.db_path.exists():
            try:
                os.chmod(self.db_path, file_mode)
            except OSError:
                pass

    def register_file(
        self,
        source_path: Path,
        category: str = "inbox",
        relative_subpath: str | None = None,
        copy_mode: Literal["copy", "move", "in_place"] = "copy",
        origin: str | None = None,
        source_url: str | None = None,
        tags: list[str] | None = None,
        brain_note_id: str | None = None,
        retention: RetentionType = "keep",
        ttl_seconds: float | None = None,
        expires_at: str | None = None,
        visibility: VisibilityType = "private",
        deduplicate: bool = True,
    ) -> tuple[AssetRecord, bool]:
        """Register a file into the assets store.

        Returns a tuple of (AssetRecord, is_new: bool).
        If deduplicate=True and an asset with matching sha256 exists, returns existing.
        """
        self.ensure_structure()
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Source file does not exist: {source}")

        sha256 = compute_sha256(source)
        size_bytes = source.stat().st_size
        mime_type = detect_mime_type(source)

        if deduplicate:
            existing = self.find_by_sha256(sha256)
            if existing:
                return existing[0], False

        if copy_mode == "in_place":
            try:
                rel_path = str(source.relative_to(self.root))
                target_path = source
            except ValueError:
                raise ValueError(
                    f"File {source} must be inside {self.root} "
                    "for in_place registration"
                ) from None
        else:
            filename = source.name
            target_dir = self.root / category
            if relative_subpath:
                target_dir = target_dir / relative_subpath
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / filename

            if target_path.exists() and target_path != source:
                stem = source.stem
                suffix = source.suffix
                unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
                target_path = target_dir / unique_name

            if copy_mode == "copy":
                shutil.copy2(source, target_path)
            elif copy_mode == "move":
                shutil.move(str(source), str(target_path))

            rel_path = str(target_path.relative_to(self.root))

        calculated_expires: str | None = expires_at
        if retention == "ttl" and not calculated_expires:
            seconds = (
                ttl_seconds if ttl_seconds is not None else 86400 * 7
            )  # 7 days default
            exp_time = datetime.now(UTC) + timedelta(seconds=seconds)
            calculated_expires = exp_time.isoformat()

        record = AssetRecord(
            asset_id=str(uuid.uuid4()),
            path=rel_path,
            filename=target_path.name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            created_at=datetime.now(UTC).isoformat(),
            origin=origin,
            source_url=source_url,
            tags=tags or [],
            brain_note_id=brain_note_id,
            retention=retention,
            expires_at=calculated_expires,
            visibility=visibility,
        )

        self._append_manifest(record)
        self._insert_index(record)

        return record, True

    def _append_manifest(self, record: AssetRecord) -> None:
        """Append asset record to manifest.jsonl (append-only audit log)."""
        line = record.model_dump_json() + "\n"
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def _insert_index(self, record: AssetRecord) -> None:
        """Insert asset record into index.db SQLite table."""
        with self._get_db_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assets (
                    asset_id, path, filename, mime_type, size_bytes,
                    sha256, created_at, origin, source_url, tags,
                    brain_note_id, retention, expires_at, visibility
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.asset_id,
                    record.path,
                    record.filename,
                    record.mime_type,
                    record.size_bytes,
                    record.sha256,
                    record.created_at,
                    record.origin,
                    record.source_url,
                    json.dumps(record.tags),
                    record.brain_note_id,
                    record.retention,
                    record.expires_at,
                    record.visibility,
                ),
            )
            conn.commit()

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        """Fetch asset by its UUID."""
        if not self.db_path.exists():
            return None
        with self._get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    def find_by_sha256(self, sha256: str) -> list[AssetRecord]:
        """Find registered assets by SHA256 content hash."""
        if not self.db_path.exists():
            return []
        with self._get_db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM assets WHERE sha256 = ?", (sha256,)
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def search_assets(
        self,
        origin: str | None = None,
        tag: str | None = None,
        retention: RetentionType | None = None,
        visibility: VisibilityType | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[AssetRecord]:
        """Search indexed assets matching filter criteria."""
        if not self.db_path.exists():
            return []

        clauses: list[str] = []
        params: list[Any] = []

        if origin:
            clauses.append("origin LIKE ?")
            params.append(f"%{origin}%")
        if retention:
            clauses.append("retention = ?")
            params.append(retention)
        if visibility:
            clauses.append("visibility = ?")
            params.append(visibility)
        if category:
            clauses.append("path LIKE ?")
            params.append(f"{category}/%")
        if tag:
            clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')

        where_stmt = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM assets {where_stmt} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._get_db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_record(row) for row in rows]

    def purge_expired(
        self,
        target_category: str | None = "tmp",
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Purge expired assets based on retention='ttl' and expires_at <= UTC.

        If target_category is specified (default 'tmp'), only purges assets
        in that directory.
        """
        now_iso = datetime.now(UTC).isoformat()
        if not self.db_path.exists():
            return []

        query = (
            "SELECT * FROM assets WHERE retention = 'ttl' "
            "AND expires_at IS NOT NULL AND expires_at <= ?"
        )
        params: list[Any] = [now_iso]
        if target_category:
            query += " AND path LIKE ?"
            params.append(f"{target_category}/%")

        with self._get_db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            expired = [self._row_to_record(row) for row in rows]

            purged: list[dict[str, Any]] = []
            for item in expired:
                file_path = self.root / item.path
                item_info = {
                    "asset_id": item.asset_id,
                    "path": item.path,
                    "sha256": item.sha256,
                    "expires_at": item.expires_at,
                    "file_deleted": False,
                }
                if not dry_run:
                    if file_path.is_file():
                        try:
                            file_path.unlink()
                            item_info["file_deleted"] = True
                        except OSError as e:
                            item_info["error"] = str(e)
                    conn.execute(
                        "DELETE FROM assets WHERE asset_id = ?", (item.asset_id,)
                    )
                purged.append(item_info)

            if not dry_run:
                conn.commit()

        return purged

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AssetRecord:
        """Convert a SQLite row to AssetRecord."""
        raw_tags = row["tags"]
        try:
            tags = json.loads(raw_tags) if raw_tags else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        return AssetRecord(
            asset_id=row["asset_id"],
            path=row["path"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            created_at=row["created_at"],
            origin=row["origin"],
            source_url=row["source_url"],
            tags=tags,
            brain_note_id=row["brain_note_id"],
            retention=row["retention"],
            expires_at=row["expires_at"],
            visibility=row["visibility"],
        )
