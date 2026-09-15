-- SQLite Schema for Exocortex Orchestrator Assets Store Index
-- Default location: /home/fsirio/assets/.meta/index.db

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
    retention TEXT NOT NULL DEFAULT 'keep', -- 'keep' | 'ttl'
    expires_at TEXT,                       -- ISO 8601 UTC timestamp if retention is 'ttl'
    visibility TEXT NOT NULL DEFAULT 'private' -- 'private' | 'shareable'
);

CREATE INDEX IF NOT EXISTS idx_assets_sha256 ON assets(sha256);
CREATE INDEX IF NOT EXISTS idx_assets_origin ON assets(origin);
CREATE INDEX IF NOT EXISTS idx_assets_path ON assets(path);
CREATE INDEX IF NOT EXISTS idx_assets_created_at ON assets(created_at);
CREATE INDEX IF NOT EXISTS idx_assets_retention_expires ON assets(retention, expires_at);
CREATE INDEX IF NOT EXISTS idx_assets_visibility ON assets(visibility);
CREATE INDEX IF NOT EXISTS idx_assets_brain_note ON assets(brain_note_id);
