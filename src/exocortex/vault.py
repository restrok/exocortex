"""Canonical Markdown vault persistence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from exocortex.models import NoteMetadata, VaultNote

_MANAGED_START = "<!-- codex-brain:managed:start -->"
_MANAGED_END = "<!-- codex-brain:managed:end -->"
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


class Vault:
    """Read and write canonical notes while preserving user-authored content."""

    def __init__(self, root: Path) -> None:
        """Initialize a vault rooted at the canonical Vault directory."""
        self._root = root

    def ensure_exists(self) -> None:
        """Create the Vault directory."""
        self._root.mkdir(parents=True, exist_ok=True)

    def iter_notes(self, space_id: str | None = None) -> Iterable[VaultNote]:
        """Yield validated notes, optionally limited to one knowledge space."""
        if not self._root.exists():
            return
        for path in sorted(self._root.rglob("*.md")):
            note = self._read_path(path)
            if note and (space_id is None or note.metadata.space_id == space_id):
                yield note

    def get(self, note_id: str) -> VaultNote | None:
        """Find one note by its stable identifier."""
        for note in self.iter_notes():
            if str(note.metadata.id) == note_id:
                return note
        return None

    def get_many(
        self,
        note_ids: Iterable[str],
        space_id: str | None = None,
    ) -> dict[str, VaultNote]:
        """Find several notes with one canonical-vault scan."""
        requested = {str(note_id) for note_id in note_ids}
        if not requested:
            return {}
        return {
            str(note.metadata.id): note
            for note in self.iter_notes(space_id)
            if str(note.metadata.id) in requested
        }

    def find_by_source_hash(self, source_hash: str) -> VaultNote | None:
        """Find a note created from a sanitized source hash."""
        for note in self.iter_notes():
            if any(
                reference.content_hash == source_hash
                for reference in note.metadata.source_refs
            ):
                return note
        return None

    def find_by_source_id(self, source_id: str) -> VaultNote | None:
        """Find a note created from a stable external source identifier."""
        for note in self.iter_notes():
            if any(
                reference.id == source_id for reference in note.metadata.source_refs
            ):
                return note
        return None

    def upsert_managed(self, metadata: NoteMetadata, managed_content: str) -> VaultNote:
        """Create or update a managed block without overwriting human edits."""
        existing = self.get(str(metadata.id))
        if existing is None:
            existing = (
                self.find_by_source_hash(metadata.source_refs[0].content_hash)
                if metadata.source_refs
                else None
            )

        if existing:
            metadata.id = existing.metadata.id
            metadata.created_at = existing.metadata.created_at
            content = _replace_managed_block(existing.content, managed_content)
            path = self._absolute_path(existing.path)
            desired_path = self._new_path(metadata)
            if desired_path != path:
                if desired_path.exists():
                    metadata.recommendation_state = "quarantined"
                else:
                    desired_path.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(desired_path)
                    path = desired_path
        else:
            content = _managed_block(managed_content)
            path = self._new_path(metadata)

        metadata.updated_at = datetime.now(UTC)
        self._write(path, metadata, content)
        return VaultNote(
            metadata=metadata,
            content=content,
            path=self._relative_path(path),
        )

    def update_metadata(self, note: VaultNote) -> VaultNote:
        """Persist frontmatter changes without changing human or managed content."""
        note.metadata.updated_at = datetime.now(UTC)
        self._write(self._absolute_path(note.path), note.metadata, note.content)
        return note

    def write_review(
        self,
        source_id: str,
        content: str,
        metadata: dict[str, object],
    ) -> Path:
        """Persist a review candidate outside the canonical vault."""
        review_root = self._root.parent / "Sources" / "Review"
        review_root.mkdir(parents=True, exist_ok=True)
        path = review_root / f"{_slugify(source_id)}.md"
        post = frontmatter.Post(content, **metadata)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return path

    def read_review(self, source_id: str) -> frontmatter.Post | None:
        """Read a review candidate by source identifier."""
        path = self._root.parent / "Sources" / "Review" / f"{_slugify(source_id)}.md"
        if not path.exists():
            return None
        return frontmatter.load(path)

    def remove_review(self, source_id: str) -> None:
        """Remove a review candidate after explicit rejection or promotion."""
        path = self._root.parent / "Sources" / "Review" / f"{_slugify(source_id)}.md"
        if path.exists():
            path.unlink()

    def _read_path(self, path: Path) -> VaultNote | None:
        """Parse one Markdown file, skipping non-managed notes with invalid metadata."""
        try:
            post = frontmatter.load(path)
            metadata = NoteMetadata.model_validate(post.metadata)
        except (OSError, ValueError):
            return None
        return VaultNote(
            metadata=metadata,
            content=post.content,
            path=self._relative_path(path),
        )

    def _write(self, path: Path, metadata: NoteMetadata, content: str) -> None:
        """Serialize note metadata and content atomically enough for local writes."""
        path.parent.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(content, **metadata.model_dump(mode="json"))
        temporary_path = path.with_suffix(".md.tmp")
        temporary_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        temporary_path.replace(path)

    def _new_path(self, metadata: NoteMetadata) -> Path:
        """Return the deterministic location for a new note."""
        category = f"{metadata.type.title()}s"
        filename = f"{_slugify(metadata.title)}-{str(metadata.id)[:8]}.md"
        return self._root / metadata.space_id / category / filename

    def _relative_path(self, path: Path) -> str:
        """Return a path relative to the Brain data directory."""
        return str(path.relative_to(self._root.parent))

    def _absolute_path(self, relative_path: str) -> Path:
        """Resolve a stored data-relative note path."""
        return self._root.parent / relative_path


def extract_links(note: VaultNote) -> list[str]:
    """Return explicit and Markdown wikilinks from a note."""
    links = set(note.metadata.links)
    links.update(match.group(1).strip() for match in _WIKILINK.finditer(note.content))
    return sorted(link for link in links if link)


def _managed_block(content: str) -> str:
    """Wrap generated content in a block safe to replace later."""
    return f"{_MANAGED_START}\n{content.strip()}\n{_MANAGED_END}\n"


def _replace_managed_block(existing: str, replacement: str) -> str:
    """Replace the managed block or append one while retaining manual text."""
    block = _managed_block(replacement)
    start = existing.find(_MANAGED_START)
    end = existing.find(_MANAGED_END)
    if start >= 0 and end >= start:
        end += len(_MANAGED_END)
        return f"{existing[:start]}{block}{existing[end:]}".strip() + "\n"
    separator = "" if not existing.strip() else "\n\n"
    return f"{existing.rstrip()}{separator}{block}"


def _slugify(value: str) -> str:
    """Create a stable filesystem-safe slug."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "untitled"
