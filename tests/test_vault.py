"""Tests for canonical Markdown preservation."""

from pathlib import Path

from exocortex.models import NoteMetadata, SourceReference
from exocortex.vault import Vault


def test_upsert_managed_preserves_user_authored_text(tmp_path: Path) -> None:
    """Generated updates replace only the managed block."""
    vault = Vault(tmp_path / "brain" / "Vault")
    vault.ensure_exists()
    metadata = NoteMetadata(
        type="decision",
        title="Use Neo4j",
        space_id="work",
        source_refs=[
            SourceReference(
                id="task-1",
                locator="task://1",
                content_hash="hash-1",
            )
        ],
    )
    note = vault.upsert_managed(metadata, "Initial generated detail.")
    path = tmp_path / "brain" / note.path
    path.write_text(
        path.read_text(encoding="utf-8") + "\nMy human clarification.\n",
        encoding="utf-8",
    )

    updated = vault.upsert_managed(metadata, "Updated generated detail.")

    assert updated.metadata.id == note.metadata.id
    content = path.read_text(encoding="utf-8")
    assert "Updated generated detail." in content
    assert "Initial generated detail." not in content
    assert "My human clarification." in content


def test_upsert_managed_moves_a_note_when_type_changes(tmp_path: Path) -> None:
    """Managed updates keep the filesystem path aligned with the current type."""
    vault = Vault(tmp_path / "brain" / "Vault")
    note = vault.upsert_managed(
        NoteMetadata(type="task", title="A migration", space_id="work"),
        "## Summary\nDo the migration.",
    )
    old_path = tmp_path / "brain" / note.path

    note.metadata.type = "decision"
    moved = vault.upsert_managed(note.metadata, "## Summary\nChoose the migration.")

    assert moved.path.startswith("Vault/work/Decisions/")
    assert not old_path.exists()


def test_get_many_scans_the_vault_once(tmp_path: Path, monkeypatch) -> None:
    """Bulk note lookup avoids one full Markdown scan per result."""
    vault = Vault(tmp_path / "brain" / "Vault")
    notes = [
        vault.upsert_managed(
            NoteMetadata(type="task", title=f"Task {index}", space_id="work"),
            f"## Summary\nTask {index}.",
        )
        for index in range(3)
    ]
    original_iter_notes = vault.iter_notes
    scan_count = 0

    def counted_iter_notes(space_id=None):
        nonlocal scan_count
        scan_count += 1
        yield from original_iter_notes(space_id)

    monkeypatch.setattr(vault, "iter_notes", counted_iter_notes)

    found = vault.get_many([str(note.metadata.id) for note in notes])

    assert set(found) == {str(note.metadata.id) for note in notes}
    assert scan_count == 1
