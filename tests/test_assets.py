"""Unit tests for the Exocortex assets store manager and indexing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exocortex.assets import AssetStore


@pytest.fixture
def asset_store(tmp_path: Path) -> AssetStore:
    """Provide a fresh AssetStore instance rooted in a temporary test directory."""
    root = tmp_path / "assets"
    store = AssetStore(root)
    store.ensure_structure()
    return store


def test_ensure_structure_creates_directories_and_meta(asset_store: AssetStore) -> None:
    """Verify all required subdirectories, manifest, and SQLite index are created."""
    for category in AssetStore.CATEGORIES:
        cat_dir = asset_store.root / category
        assert cat_dir.is_dir()
        # Verify permissions: 0o750 (owner rwx, group rx, others none)
        assert oct(cat_dir.stat().st_mode)[-3:] == "750"

    assert asset_store.manifest_path.is_file()
    assert asset_store.db_path.is_file()


def test_register_file_and_deduplication(
    asset_store: AssetStore, tmp_path: Path
) -> None:
    """Test file registration, sha256 calculation, and hash deduplication."""
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("Hello Exocortex Assets!", encoding="utf-8")

    record, is_new = asset_store.register_file(
        source_path=sample_file,
        category="docs",
        origin="agent:test_runner",
        tags=["unit-test", "greeting"],
        retention="keep",
        visibility="private",
    )

    assert is_new is True
    assert record.filename == "sample.txt"
    assert record.path.startswith("docs/")
    assert (asset_store.root / record.path).is_file()
    assert record.size_bytes == len("Hello Exocortex Assets!")
    assert record.tags == ["unit-test", "greeting"]
    assert record.retention == "keep"
    assert record.visibility == "private"

    # Verify manifest.jsonl contains the record
    lines = asset_store.manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    manifest_data = json.loads(lines[0])
    assert manifest_data["asset_id"] == record.asset_id
    assert manifest_data["sha256"] == record.sha256

    # Verify query by ID and hash
    fetched = asset_store.get_asset(record.asset_id)
    assert fetched is not None
    assert fetched.asset_id == record.asset_id

    by_hash = asset_store.find_by_sha256(record.sha256)
    assert len(by_hash) == 1
    assert by_hash[0].asset_id == record.asset_id

    # Test deduplication: registering the same file again returns existing record
    dup_file = tmp_path / "copy_of_sample.txt"
    dup_file.write_text("Hello Exocortex Assets!", encoding="utf-8")
    record_dup, is_new_dup = asset_store.register_file(
        source_path=dup_file,
        category="inbox",
        deduplicate=True,
    )
    assert is_new_dup is False
    assert record_dup.asset_id == record.asset_id


def test_search_assets(asset_store: AssetStore, tmp_path: Path) -> None:
    """Test searching assets by origin, tags, category, and retention."""
    f1 = tmp_path / "doc1.md"
    f1.write_text("# Doc 1", encoding="utf-8")
    asset_store.register_file(
        source_path=f1,
        category="docs",
        origin="agent:writer",
        tags=["markdown", "rfc"],
    )

    f2 = tmp_path / "img1.png"
    f2.write_bytes(b"\x89PNG\r\n\x1a\nfakecontent")
    asset_store.register_file(
        source_path=f2,
        category="media",
        origin="telegram:user123",
        tags=["screenshot"],
    )

    by_origin = asset_store.search_assets(origin="telegram")
    assert len(by_origin) == 1
    assert by_origin[0].filename == "img1.png"

    by_tag = asset_store.search_assets(tag="markdown")
    assert len(by_tag) == 1
    assert by_tag[0].filename == "doc1.md"

    by_category = asset_store.search_assets(category="media")
    assert len(by_category) == 1
    assert by_category[0].filename == "img1.png"


def test_purge_expired_tmp_assets(asset_store: AssetStore, tmp_path: Path) -> None:
    """Test purging expired assets from tmp/ based on TTL."""
    expired_file = tmp_path / "expired.log"
    expired_file.write_text("temporary debug log", encoding="utf-8")
    past_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    expired_record, _ = asset_store.register_file(
        source_path=expired_file,
        category="tmp",
        retention="ttl",
        expires_at=past_iso,
    )

    valid_file = tmp_path / "keep.log"
    valid_file.write_text("future log", encoding="utf-8")
    future_iso = (datetime.now(UTC) + timedelta(hours=24)).isoformat()

    valid_record, _ = asset_store.register_file(
        source_path=valid_file,
        category="tmp",
        retention="ttl",
        expires_at=future_iso,
    )

    # Dry run purge: reports without deleting
    dry_results = asset_store.purge_expired(target_category="tmp", dry_run=True)
    assert len(dry_results) == 1
    assert dry_results[0]["asset_id"] == expired_record.asset_id
    assert (asset_store.root / expired_record.path).is_file()

    # Actual purge
    purged_results = asset_store.purge_expired(target_category="tmp", dry_run=False)
    assert len(purged_results) == 1
    assert purged_results[0]["asset_id"] == expired_record.asset_id
    assert not (asset_store.root / expired_record.path).exists()
    assert (asset_store.root / valid_record.path).is_file()

    # Verify purged asset is no longer in index.db
    assert asset_store.get_asset(expired_record.asset_id) is None
    assert asset_store.get_asset(valid_record.asset_id) is not None
