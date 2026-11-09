"""Session ingestion checkpointing and deterministic note fingerprinting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from exocortex.actions import canonicalize_action_key
from exocortex.ingest import TRIVIAL_FILTER_VERSION
from exocortex.models import ActionSignature, VaultNote


def _effective_labels(note: VaultNote) -> list[str]:
    """Return generated and human labels without duplicates."""
    return sorted(set(note.metadata.labels + note.metadata.manual_labels))


def _note_actions(note: VaultNote) -> list[ActionSignature]:
    """Return normalized actions attached to a note."""
    actions: list[ActionSignature] = []
    for action in note.metadata.actions:
        actions.append(
            action.model_copy(
                update={"canonical_action_key": canonicalize_action_key(action)}
            )
        )
    return actions


def _sessions_root_id(root: Path) -> str:
    """Return a stable opaque identity for one mounted sessions root."""
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _session_checkpoint_key(root: Path, path: Path) -> str:
    """Return a portable relative rollout key for the checkpoint."""
    return path.relative_to(root).as_posix()


def _session_signature(path: Path) -> dict[str, int] | None:
    """Return file metadata used to detect a changed rollout."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def _checkpoint_matches(
    entry: object,
    signature: dict[str, int] | None,
) -> bool:
    """Return whether a checkpoint entry still describes the current file."""
    if signature is None or not isinstance(entry, dict):
        return False
    return (
        _checkpoint_entry_is_complete(entry)
        and entry.get("mtime_ns") == signature["mtime_ns"]
        and entry.get("size") == signature["size"]
    )


def _checkpoint_entry_is_complete(entry: object) -> bool:
    """Return whether a checkpoint is complete without fallback records."""
    if not isinstance(entry, dict) or entry.get("status") != "completed":
        return False
    return not any(
        record.get("status") in {"fallback", "promoted_fallback"}
        for record in _checkpoint_record_state(entry).values()
    )


def _checkpoint_record_state(entry: object) -> dict[str, dict[str, Any]]:
    """Return per-segment checkpoint state from a session entry."""
    if not isinstance(entry, dict):
        return {}
    records = entry.get("records")
    if not isinstance(records, dict):
        return {}
    return {
        str(source_id): value
        for source_id, value in records.items()
        if isinstance(value, dict)
    }


def _checkpoint_record_matches(entry: object, content_hash: str) -> bool:
    """Return whether a segment was completed with the current filter version."""
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("content_hash") == content_hash
        and entry.get("filter_version") == TRIVIAL_FILTER_VERSION
        and entry.get("status")
        in {
            "already_indexed",
            "extracted",
            "promoted",
            "promoted_quarantined",
            "skipped_trivial",
        }
    )


def _update_ingest_summary(summary: dict[str, Any], status: str) -> None:
    """Update aggregate counters for one ingestion result."""
    if status == "failed":
        summary["records_failed"] = int(summary["records_failed"]) + 1
        return
    summary["records_processed"] = int(summary["records_processed"]) + 1
    if status == "already_indexed":
        summary["already_indexed"] = int(summary["already_indexed"]) + 1
    if status in {"extracted", "promoted", "promoted_quarantined"}:
        summary["extracted"] = int(summary.get("extracted", 0)) + 1
        summary["promoted"] = int(summary["promoted"]) + 1
    if status in {"fallback", "promoted_fallback"}:
        summary["fallback"] = int(summary["fallback"]) + 1


def _note_fingerprint(note: VaultNote) -> str:
    """Return a stable fingerprint for reflection-relevant note content."""
    payload = {
        "id": str(note.metadata.id),
        "space_id": note.metadata.space_id,
        "owner": note.metadata.owner,
        "type": note.metadata.type,
        "title": note.metadata.title,
        "labels": _effective_labels(note),
        "evidence_status": note.metadata.evidence_status,
        "confidence": note.metadata.confidence,
        "usage_count": note.metadata.usage_count,
        "success_count": note.metadata.success_count,
        "last_feedback_at": (
            note.metadata.last_feedback_at.isoformat()
            if note.metadata.last_feedback_at
            else None
        ),
        "last_feedback_notes": note.metadata.last_feedback_notes,
        "recommendation_state": note.metadata.recommendation_state,
        "extraction_status": note.metadata.extraction_status,
        "prompt_version": note.metadata.prompt_version,
        "model_version": note.metadata.model_version,
        "source_ids": sorted(reference.id for reference in note.metadata.source_refs),
        "claims": [claim.model_dump(mode="json") for claim in note.metadata.claims],
        "actions": [action.model_dump(mode="json") for action in _note_actions(note)],
        "content": note.content,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _index_fingerprint(note: VaultNote, embed: bool, embedding_model: str) -> str:
    """Include the projection mode in the incremental index fingerprint."""
    mode = f"embedding:{embedding_model}" if embed else "lexical"
    return f"{_note_fingerprint(note)}|{mode}"
