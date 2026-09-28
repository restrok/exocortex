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


def test_iter_notes_multi_spaces(tmp_path: Path) -> None:
    """iter_notes supports single space, collections (list, set, tuple), and None."""
    vault = Vault(tmp_path / "brain" / "Vault")
    vault.ensure_exists()
    n_work = vault.upsert_managed(
        NoteMetadata(type="task", title="Task Work", space_id="work"),
        "## Summary\nWork.",
    )
    n_shared = vault.upsert_managed(
        NoteMetadata(type="task", title="Task Shared", space_id="shared"),
        "## Summary\nShared.",
    )
    n_fsirio = vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Task Fsirio",
            space_id="personal-fsirio",
            owner="fsirio",
        ),
        "## Summary\nFsirio.",
    )
    n_mercedes = vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Task Mercedes",
            space_id="personal-mercedes",
            owner="mercedes",
        ),
        "## Summary\nMercedes.",
    )

    # 1. Single space string
    work_notes = list(vault.iter_notes("work"))
    assert len(work_notes) == 1
    assert work_notes[0].metadata.id == n_work.metadata.id

    # 2. List of spaces
    fsirio_and_shared = list(vault.iter_notes(["personal-fsirio", "shared"]))
    assert len(fsirio_and_shared) == 2
    assert {n.metadata.id for n in fsirio_and_shared} == {
        n_fsirio.metadata.id,
        n_shared.metadata.id,
    }

    # 3. Set of spaces
    mercedes_notes = list(vault.iter_notes(set(["personal-mercedes"])))
    assert len(mercedes_notes) == 1
    assert mercedes_notes[0].metadata.id == n_mercedes.metadata.id

    # 4. None yields all notes
    all_notes = list(vault.iter_notes())
    assert len(all_notes) == 4

    # 5. Empty collection yields nothing
    empty_notes = list(vault.iter_notes([]))
    assert len(empty_notes) == 0


def test_owner_field_preservation(tmp_path: Path) -> None:
    """The owner field in NoteMetadata is preserved across frontmatter writes."""
    import frontmatter

    vault = Vault(tmp_path / "brain" / "Vault")
    vault.ensure_exists()
    metadata = NoteMetadata(
        type="task",
        title="Personal Task",
        space_id="personal-fsirio",
        owner="fsirio",
    )
    note = vault.upsert_managed(metadata, "## Summary\nOwner test.")
    file_path = tmp_path / "brain" / note.path

    # Read raw frontmatter
    post = frontmatter.load(file_path)
    assert post.metadata.get("owner") == "fsirio"

    # Re-read through vault
    loaded = vault.get(str(note.metadata.id))
    assert loaded is not None
    assert loaded.metadata.owner == "fsirio"

    # Update metadata
    loaded.metadata.confidence = 0.88
    vault.update_metadata(loaded)

    # Verify owner is still present after update
    post_updated = frontmatter.load(file_path)
    assert post_updated.metadata.get("owner") == "fsirio"
    assert post_updated.metadata.get("confidence") == 0.88
