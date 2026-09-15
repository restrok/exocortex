# Exocortex Orchestrator Assets Store Conventions

## 1. Architectural Principles & Runtime Decoupling

The Exocortex Assets Store provides persistent, structured, and auditable storage for assets handled by the orchestrator, autonomous agents, Telegram bots, and background workers.

In strict adherence to the project's architectural principles:
- **Code/Runtime Decoupling**: Git repositories never contain user assets, generated outputs, runtime state, or secrets.
- **Runtime Host Location**: All runtime data resides strictly on the host under `/home/fsirio/assets/`.
- **Filesystem as Primary Authority**: The filesystem layout and an append-only JSONL manifest serve as the immutable source of truth, complemented by a fast SQLite index for instant lookup and deduplication.

---

## 2. Directory Structure

The assets root directory is located at `/home/fsirio/assets/` (configurable via `$EXOCORTEX_ASSETS_DIR`):

```text
/home/fsirio/assets/
├── inbox/        # Raw incoming Telegram uploads, downloads, external payloads
├── generated/    # Outputs from workers, agents, workflows, and synthesis jobs
├── media/        # Images, audio, voice notes, video clips
├── docs/         # PDFs, markdown notes, text documents, exports
├── tmp/          # Ephemeral workspace files subjected to automated TTL purging
└── .meta/        # Metadata index, append-only manifests, and maintenance scripts
    ├── manifest.jsonl   # Append-only JSON Lines ledger of every registered asset
    ├── index.db         # SQLite search index and deduplication cache
    ├── purge_assets.py  # Standalone purge script
    └── purge_tmp.sh     # Wrapper for automated cron/systemd purge execution
```

### Folder Roles
- **`inbox/`**: Ingestion landing zone for raw, unverified files received from Telegram, email integrations, or HTTP webhooks.
- **`generated/`**: Artifacts produced by autonomous agents or background workers (reports, diagrams, converted media).
- **`media/`**: Processed and categorized images, audio, voice notes, and video files.
- **`docs/`**: Long-term documents such as research papers, PDFs, markdown exports, and text summaries.
- **`tmp/`**: Scratch directory for ephemeral intermediate files, transient worker caches, and staging areas.
- **`.meta/`**: Administrative metadata directory storing the append-only ledger, the SQLite index database, and local maintenance scripts.

---

## 3. Metadata Specification

The metadata architecture uses a dual-layer model:
1. **`manifest.jsonl` (Append-Only Ledger)**: An immutable record where every registered asset is appended. If the SQLite database is ever corrupted or deleted, `manifest.jsonl` allows complete index rebuilding.
2. **`index.db` (SQLite Index)**: A lightweight database optimized for fast queries by tag, origin, and content hash (deduplication).

### Asset Metadata Fields

| Field | Type | Description |
| :--- | :--- | :--- |
| `asset_id` | `UUID (string)` | Unique identifier for the asset (RFC 4122 v4). |
| `path` | `string` | Relative path inside the assets root (e.g., `media/photo.jpg`). |
| `filename` | `string` | Base filename of the asset. |
| `mime_type` | `string` | Standard MIME type (e.g., `image/jpeg`, `application/pdf`). |
| `size_bytes` | `integer` | File size in bytes. |
| `sha256` | `string` | 64-character SHA-256 hexadecimal digest of the file contents. |
| `created_at` | `string` | UTC creation timestamp in ISO 8601 format (`YYYY-MM-DDTHH:MM:SS.mmmmmmZ`). |
| `origin` | `string` | Provenance identifier (`agent/task_id/session_id/user`). |
| `source_url` | `string \| null` | Original remote download URL or origin link, if applicable. |
| `tags` | `array[string]` | List of classification tags (e.g., `["telegram", "receipt"]`). |
| `brain_note_id` | `string \| null` | Optional reference linking to a Codex/Exocortex canonical note. |
| `retention` | `string` | Retention policy: `keep` (permanent) or `ttl` (ephemeral). |
| `expires_at` | `string \| null` | Expiration timestamp in UTC ISO 8601 (required if `retention=ttl`). |
| `visibility` | `string` | Visibility scope: `private` (default) or `shareable`. |

---

## 4. Retention and Purge Policy

- **`keep`**: Permanent retention. The asset is preserved indefinitely until explicitly deleted.
- **`ttl`**: Ephemeral retention with an automated expiration deadline (`expires_at`).
  - Assets placed in `tmp/` default to a 7-day TTL unless a custom TTL is supplied at registration.
- **Purge Process**:
  - The purge script evaluates assets where `retention = 'ttl'` and `expires_at <= current_utc_timestamp`.
  - Expired files are unlinked from the filesystem.
  - Expired records are pruned from `index.db`.
  - Supports `--dry-run` to preview deletions without altering data.

---

## 5. Permissions and Security

- **Directories**: `750` (`rwxr-x---`). Restricted exclusively to owner `fsirio` and group `fsirio`.
- **Files**: `640` (`rw-r-----`). Files can only be read and written by owner `fsirio` and read by group members.
- **Security Boundaries**: Web or non-root agent processes running under different credentials cannot browse or read assets unless explicitly granted group access.

---

## 6. Tooling & Usage

### Bootstrap (Idempotent Setup)
To initialize the directory structure and SQLite index on the host:
```bash
./scripts/bootstrap_assets.sh
```

### CLI Operations (Exocortex CLI)
The store is integrated into the `exocortex` command-line suite:

```bash
# Bootstrap store
uv run exocortex assets bootstrap

# Register an asset
uv run exocortex assets register /path/to/download.pdf \
    --category docs \
    --origin "agent:researcher:task-42" \
    --tags "research,ai" \
    --retention keep

# Register an ephemeral file in tmp/ with 24-hour TTL
uv run exocortex assets register /tmp/job_output.json \
    --category tmp \
    --retention ttl \
    --ttl-seconds 86400

# Search assets by tag or origin
uv run exocortex assets search --tag research
uv run exocortex assets search --origin telegram

# Find by SHA-256 (deduplication check)
uv run exocortex assets find-by-hash <sha256-hash>

# Purge expired tmp files
uv run exocortex assets purge --category tmp
uv run exocortex assets purge --dry-run
```

### Automated Purge via Cron / Systemd
A scheduled cron job or systemd timer can run the standalone purge script periodically:
```crontab
0 * * * * /home/fsirio/assets/.meta/purge_tmp.sh > /dev/null 2>&1
```
