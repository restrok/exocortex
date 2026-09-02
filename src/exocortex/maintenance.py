"""Repair, backup, and resumable backfill operations for Codex Brain."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tarfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import httpx

from exocortex.codex_sessions import CodexSessionAdapter
from exocortex.gateway import EXTRACTION_PROMPT_VERSION, GatewayError
from exocortex.ingest import SourceRecord
from exocortex.models import VaultNote

if TYPE_CHECKING:
    from exocortex.service import BrainService


_LOGGER = logging.getLogger(__name__)


def repair_report(service: BrainService) -> dict[str, object]:
    """Return deterministic path, state, and duplicate repair candidates."""
    notes = list(service.vault.iter_notes())
    moves: list[dict[str, str]] = []
    state_changes: list[dict[str, str]] = []
    metadata_changes: list[str] = []
    for note in notes:
        if _metadata_needs_sanitization(service, note):
            metadata_changes.append(str(note.metadata.id))
        expected = service.vault._new_path(note.metadata)  # pylint: disable=protected-access
        expected_relative = str(expected.relative_to(service.settings.data_dir))
        if expected_relative != note.path:
            moves.append(
                {
                    "note_id": str(note.metadata.id),
                    "from": note.path,
                    "to": expected_relative,
                }
            )
        desired_state = _desired_state(note)
        if desired_state != note.metadata.recommendation_state:
            state_changes.append(
                {
                    "note_id": str(note.metadata.id),
                    "from": note.metadata.recommendation_state,
                    "to": desired_state,
                }
            )
    duplicates = _duplicate_notes(notes)
    return {
        "status": "ok",
        "notes": len(notes),
        "moves": moves,
        "state_changes": state_changes,
        "metadata_changes": metadata_changes,
        "duplicates": duplicates,
    }


def repair_apply(service: BrainService) -> dict[str, object]:
    """Backup and apply deterministic repairs without deleting ambiguous notes."""
    report = repair_report(service)
    backup = _create_backup(service, report)
    moved = 0
    changed = 0
    quarantined = 0
    notes = list(service.vault.iter_notes())
    duplicate_map = {
        item["duplicate_id"]: item["canonical_id"]
        for item in report["duplicates"]
        if item.get("action") == "supersede"
    }
    quarantine_ids = {
        item["duplicate_id"]
        for item in report["duplicates"]
        if item.get("action") == "quarantine"
    }
    notes_by_id = {str(note.metadata.id): note for note in notes}
    merged_ids: set[str] = set()
    for item in report["duplicates"]:
        if item.get("action") != "supersede":
            continue
        duplicate = notes_by_id.get(item["duplicate_id"])
        canonical = notes_by_id.get(item["canonical_id"])
        if duplicate is None or canonical is None:
            continue
        _merge_duplicate_metadata(canonical, duplicate)
        if str(canonical.metadata.id) not in merged_ids:
            service.vault.update_metadata(canonical)
            merged_ids.add(str(canonical.metadata.id))
    for note in notes:
        metadata_quarantined = _sanitize_note_metadata(service, note)
        expected = service.vault._new_path(note.metadata)  # pylint: disable=protected-access
        current = service.vault._absolute_path(note.path)  # pylint: disable=protected-access
        path_conflict = False
        if current != expected:
            if expected.exists():
                note.metadata.recommendation_state = "quarantined"
                path_conflict = True
                quarantined += 1
            else:
                expected.parent.mkdir(parents=True, exist_ok=True)
                current.replace(expected)
                note.path = str(expected.relative_to(service.settings.data_dir))
                moved += 1
        note.metadata.schema_version = 2
        desired_state = (
            "quarantined"
            if path_conflict or metadata_quarantined
            else _desired_state(note)
        )
        duplicate_id = str(note.metadata.id)
        if duplicate_id in quarantine_ids:
            desired_state = "quarantined"
        if duplicate_id in duplicate_map:
            desired_state = "superseded"
            note.metadata.superseded_by = duplicate_map[duplicate_id]
        if note.metadata.recommendation_state != desired_state:
            note.metadata.recommendation_state = desired_state
            changed += 1
        service.vault.update_metadata(note)
    manifest_path = backup.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "backup": str(backup),
                "report": report,
                "hashes": _vault_hashes(service),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _retain_backups(backup.parent, keep=10)
    return {
        "status": "applied",
        "backup": str(backup),
        "moved": moved,
        "changed": changed,
        "metadata_sanitized": len(report["metadata_changes"]),
        "quarantined": quarantined,
        "duplicates": len(duplicate_map),
    }


def repair_rollback(service: BrainService, backup: Path) -> dict[str, object]:
    """Restore Vault and sanitized sources from one explicit backup."""
    backup_root = (service.settings.data_dir / "backups").resolve()
    backup_path = backup.resolve()
    if not backup_path.is_relative_to(backup_root):
        raise ValueError("Backup must be inside the configured backups directory.")
    if not backup_path.exists():
        raise FileNotFoundError(backup)
    shutil.rmtree(service.settings.vault_dir, ignore_errors=True)
    shutil.rmtree(service.settings.sanitized_dir, ignore_errors=True)
    service.settings.vault_dir.mkdir(parents=True, exist_ok=True)
    service.settings.sanitized_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(backup_path, "r:gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name == "Vault"
            or member.name.startswith("Vault/")
            or member.name == "Sources/Sanitized"
            or member.name.startswith("Sources/Sanitized/")
        ]
        archive.extractall(service.settings.data_dir, members=members, filter="data")
    return {"status": "rolled_back", "backup": str(backup_path)}


def backfill_sources(
    service: BrainService,
    batch_size: int = 25,
    resume: bool = True,
    process_all: bool = False,
    max_failures: int = 25,
) -> dict[str, object]:
    """Re-extract sanitized sources in resumable batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if max_failures < 0:
        raise ValueError("max_failures must be zero or greater")
    checkpoint_path = service.settings.state_dir / "backfill-checkpoint.json"
    checkpoint = _read_checkpoint(checkpoint_path) if resume else {"completed": []}
    completed = set(checkpoint.get("completed", []))
    if process_all:
        completed = set()
    failed = {
        str(path) for path in checkpoint.get("failed", []) if isinstance(path, str)
    }
    paths = sorted(service.settings.sanitized_dir.glob("*.md"))
    pending = [path for path in paths if str(path) not in completed]
    _LOGGER.info(
        "Backfill started total=%d pending=%d batch_size=%d process_all=%s "
        "max_failures=%d",
        len(paths),
        len(pending),
        batch_size,
        process_all,
        max_failures,
    )
    try:
        service.gateway.health()
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error(
            "Backfill stopped: gateway health failed error=%s",
            error.__class__.__name__,
        )
        return {
            "status": "degraded",
            "processed": 0,
            "failed": [],
            "remaining": len(pending),
            "reason": error.__class__.__name__,
        }
    processed = 0
    skipped = 0
    run_failed: set[str] = set()
    indexed_sources = {
        reference.id: (note, reference.content_hash)
        for note in service.vault.iter_notes()
        for reference in note.metadata.source_refs
    }
    batches = (
        [
            pending[index : index + batch_size]
            for index in range(0, len(pending), batch_size)
        ]
        if process_all
        else [pending[:batch_size]]
    )
    stopped = False
    for batch_number, batch in enumerate(batches, 1):
        _LOGGER.info(
            "Backfill batch started batch=%d/%d sources=%d completed=%d remaining=%d",
            batch_number,
            len(batches),
            len(batch),
            len(completed),
            len(paths) - len(completed),
        )
        for path in batch:
            succeeded = False
            source_id = path.stem
            last_error = "UnknownError"
            for attempt in range(1, 4):
                try:
                    post = frontmatter.load(path)
                    source_id = str(post.metadata.get("source_id") or path.stem)
                    occurred_on = _parse_date(post.metadata.get("occurred_on"))
                    locator = str(post.metadata.get("locator") or path)
                    raw_record = _find_raw_record(service, locator)
                    record = SourceRecord(
                        source_id=source_id,
                        title=str(
                            post.metadata.get("title")
                            or (raw_record.title if raw_record else source_id)
                        ),
                        content=raw_record.content if raw_record else post.content,
                        space_id=str(
                            post.metadata.get("space_id")
                            or service.settings.default_space
                        ),
                        locator=locator,
                        occurred_on=occurred_on,
                    )
                    indexed_note = indexed_sources.get(source_id)
                    if not process_all and _source_is_current(
                        indexed_note,
                        post.metadata,
                    ):
                        completed.add(str(path))
                        failed.discard(str(path))
                        skipped += 1
                        succeeded = True
                        _LOGGER.info(
                            "Backfill source skipped source=%s reason=already_current",
                            path.name,
                        )
                        break
                    service._ingestor().ingest(  # pylint: disable=protected-access
                        record,
                        extract=True,
                        force_reextract=True,
                        allow_fallback=False,
                    )
                    completed.add(str(path))
                    failed.discard(str(path))
                    processed += 1
                    succeeded = True
                    break
                except (GatewayError, httpx.HTTPError) as error:
                    last_error = (
                        str(error)
                        if isinstance(error, GatewayError)
                        else error.__class__.__name__
                    )
                    if attempt < 3:
                        _LOGGER.warning(
                            "Backfill source retry source=%s attempt=%d/3 error=%s",
                            path.name,
                            attempt,
                            last_error,
                        )
                    continue
                except Exception as error:  # pylint: disable=broad-except
                    last_error = error.__class__.__name__
                    break
            if not succeeded:
                failed.add(str(path))
                run_failed.add(str(path))
                _LOGGER.error(
                    "Backfill source failed source=%s error=%s",
                    path.name,
                    last_error,
                )
                if max_failures and len(run_failed) >= max_failures:
                    stopped = True
                    _LOGGER.error(
                        "Backfill failure limit reached failures=%d limit=%d",
                        len(run_failed),
                        max_failures,
                    )
                    break
        _write_backfill_checkpoint(
            checkpoint_path,
            completed,
            sorted(failed),
            len(paths),
        )
        _LOGGER.info(
            "Backfill batch checkpointed batch=%d/%d processed=%d "
            "skipped=%d failed=%d remaining=%d",
            batch_number,
            len(batches),
            processed,
            skipped,
            len(failed),
            max(0, len(paths) - len(completed)),
        )
        if stopped:
            break
    result: dict[str, object] = {
        "status": (
            "completed"
            if not stopped and not failed and len(completed) == len(paths)
            else "partial"
        ),
        "processed": processed,
        "skipped": skipped,
        "failed": sorted(failed),
        "remaining": max(0, len(paths) - len(completed)),
    }
    if stopped:
        result["stopped"] = True
        result["stop_reason"] = "max_failures_reached"
    _LOGGER.info(
        "Backfill finished status=%s processed=%d skipped=%d failed=%d remaining=%d",
        result["status"],
        processed,
        skipped,
        len(failed),
        result["remaining"],
    )
    return result


def retry_fallback_sources(
    service: BrainService,
    batch_size: int = 25,
    process_all: bool = False,
    max_failures: int = 25,
) -> dict[str, object]:
    """Retry only notes proven to originate from deterministic fallbacks."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if max_failures < 0:
        raise ValueError("max_failures must be zero or greater")
    source_ids = _fallback_source_ids(service)
    source_paths = _sanitized_paths_by_source_id(service)
    pending_ids = sorted(source_ids.intersection(source_paths))
    selected_ids = pending_ids if process_all else pending_ids[:batch_size]
    missing_sources = sorted(source_ids - set(source_paths))
    _LOGGER.info(
        "Fallback retry started identified=%d selected=%d missing_sources=%d",
        len(source_ids),
        len(selected_ids),
        len(missing_sources),
    )
    if not selected_ids:
        return {
            "status": "completed",
            "identified": len(source_ids),
            "extracted": 0,
            "failed": [],
            "missing_sources": len(missing_sources),
            "remaining": len(pending_ids),
        }
    try:
        canary = service.extraction_canary()
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error(
            "Fallback retry stopped reason=canary_failed error=%s",
            error.__class__.__name__,
        )
        return {
            "status": "degraded",
            "reason": "canary_failed",
            "error": error.__class__.__name__,
            "identified": len(source_ids),
            "extracted": 0,
            "failed": [],
            "remaining": len(pending_ids),
        }
    if canary.get("status") != "passed":
        return {
            "status": "degraded",
            "reason": "canary_failed",
            "identified": len(source_ids),
            "extracted": 0,
            "failed": [],
            "remaining": len(pending_ids),
        }

    extracted_ids: set[str] = set()
    failed_ids: set[str] = set()
    batches = [
        selected_ids[index : index + batch_size]
        for index in range(0, len(selected_ids), batch_size)
    ]
    ingestor = service._ingestor()  # pylint: disable=protected-access
    for batch_number, source_batch in enumerate(batches, 1):
        records = [
            _record_from_sanitized_path(service, source_paths[source_id])
            for source_id in source_batch
        ]
        last_error = "UnknownError"
        for attempt in range(1, 4):
            try:
                results = ingestor.ingest_batch(
                    records,
                    extract=True,
                    force_reextract=True,
                    allow_fallback=False,
                )
                if any(result.status == "promoted_fallback" for result in results):
                    raise GatewayError("fallback returned during strict retry")
                extracted_ids.update(
                    result.source_id
                    for result in results
                    if result.status in {"promoted", "promoted_quarantined"}
                )
                failed_ids.update(
                    result.source_id
                    for result in results
                    if result.status == "failed"
                )
                break
            except (GatewayError, httpx.HTTPError) as error:
                last_error = (
                    str(error)
                    if isinstance(error, GatewayError)
                    else error.__class__.__name__
                )
                if attempt < 3:
                    _LOGGER.warning(
                        "Fallback batch retry batch=%d/%d attempt=%d/3 error=%s",
                        batch_number,
                        len(batches),
                        attempt,
                        last_error,
                    )
        else:
            failed_ids.update(source_batch)
            _LOGGER.error(
                "Fallback batch failed batch=%d/%d sources=%d error=%s",
                batch_number,
                len(batches),
                len(source_batch),
                last_error,
            )
        if max_failures and len(failed_ids) >= max_failures:
            break

    _mark_fallback_checkpoint_retried(service, extracted_ids)
    remaining = len(pending_ids) - len(extracted_ids)
    result = {
        "status": "completed" if remaining == 0 else "partial",
        "identified": len(source_ids),
        "selected": len(selected_ids),
        "extracted": len(extracted_ids),
        "failed": sorted(failed_ids),
        "missing_sources": len(missing_sources),
        "remaining": remaining,
        "canary": canary,
    }
    _LOGGER.info(
        "Fallback retry finished status=%s extracted=%d failed=%d remaining=%d",
        result["status"],
        len(extracted_ids),
        len(failed_ids),
        remaining,
    )
    return result


def _write_backfill_checkpoint(
    path: Path,
    completed: set[str],
    failed: list[str],
    total: int,
) -> None:
    """Persist progress after each backfill batch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "completed": sorted(completed),
                "failed": sorted(set(failed)),
                "remaining": max(0, total - len(completed)),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _source_is_current(
    indexed_source: tuple[VaultNote, str] | None,
    metadata: dict[str, object],
) -> bool:
    """Return whether a source already has a current canonical extraction."""
    if indexed_source is None:
        return False
    note, content_hash = indexed_source
    return (
        content_hash == str(metadata.get("content_hash") or "")
        and note.metadata.schema_version >= 2
        and note.metadata.prompt_version == EXTRACTION_PROMPT_VERSION
        and note.metadata.extraction_status != "fallback"
        and not _is_legacy_fallback_note(note)
        and not _needs_evidence_refresh(note)
    )


def _needs_evidence_refresh(note: VaultNote) -> bool:
    """Return whether an extracted note lacks any usable claim evidence."""
    claims = note.metadata.claims
    if not claims:
        return False
    return not any(
        claim.claim_type in {"user_decision", "tool_observation"}
        and claim.evidence
        and all(
            evidence.precision in {"exact", "source"}
            for evidence in claim.evidence
        )
        for claim in claims
    )


def _fallback_source_ids(service: BrainService) -> set[str]:
    """Return source IDs backed by explicit or legacy fallback evidence."""
    source_ids = {
        reference.id
        for note in service.vault.iter_notes()
        if note.metadata.extraction_status == "fallback"
        or _is_legacy_fallback_note(note)
        for reference in note.metadata.source_refs
    }
    checkpoint_path = service.settings.state_dir / "codex-ingest-checkpoint.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    sessions = checkpoint.get("sessions", {})
    if not isinstance(sessions, dict):
        return source_ids
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        records = session.get("records", {})
        if not isinstance(records, dict):
            continue
        source_ids.update(
            str(source_id)
            for source_id, record in records.items()
            if isinstance(record, dict)
            and record.get("status") in {"fallback", "promoted_fallback"}
        )
    return source_ids


def _is_legacy_fallback_note(note: VaultNote) -> bool:
    """Recognize the exact metadata emitted by the legacy fallback builder."""
    metadata = note.metadata
    return (
        metadata.extraction_status == "unknown"
        and metadata.prompt_version == EXTRACTION_PROMPT_VERSION
        and metadata.type == "task"
        and metadata.confidence == 0.3
        and metadata.evidence_status == "unknown"
        and metadata.recommendation_state in {"penalized", "quarantined"}
        and not metadata.claims
        and not metadata.labels
        and not metadata.links
    )


def _sanitized_paths_by_source_id(service: BrainService) -> dict[str, Path]:
    """Index sanitized source paths by their stored source identifier."""
    paths: dict[str, Path] = {}
    for path in service.settings.sanitized_dir.glob("*.md"):
        try:
            post = frontmatter.load(path)
        except (OSError, ValueError):
            continue
        source_id = str(post.metadata.get("source_id") or path.stem)
        paths[source_id] = path
    return paths


def _record_from_sanitized_path(service: BrainService, path: Path) -> SourceRecord:
    """Build a retry record from already-sanitized persisted source data."""
    post = frontmatter.load(path)
    source_id = str(post.metadata.get("source_id") or path.stem)
    locator = str(post.metadata.get("locator") or path)
    raw_record = _find_raw_record(service, locator)
    return SourceRecord(
        source_id=source_id,
        title=str(
            post.metadata.get("title")
            or (raw_record.title if raw_record else source_id)
        ),
        content=raw_record.content if raw_record else post.content,
        space_id=str(post.metadata.get("space_id") or service.settings.default_space),
        locator=locator,
        occurred_on=_parse_date(post.metadata.get("occurred_on")),
        session_id=(raw_record.session_id if raw_record else None),
        segment_id=(raw_record.segment_id if raw_record else None),
        event_start=(raw_record.event_start if raw_record else None),
        event_end=(raw_record.event_end if raw_record else None),
    )


def _mark_fallback_checkpoint_retried(
    service: BrainService,
    source_ids: set[str],
) -> None:
    """Mark only successfully re-extracted checkpoint records as current."""
    if not source_ids:
        return
    path = service.settings.state_dir / "codex-ingest-checkpoint.json"
    checkpoint = _read_checkpoint(path)
    sessions = checkpoint.get("sessions", {})
    if not isinstance(sessions, dict):
        return
    changed = False
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        records = session.get("records", {})
        if not isinstance(records, dict):
            continue
        for source_id in source_ids.intersection(records):
            record = records[source_id]
            if isinstance(record, dict):
                record["status"] = "promoted"
                record["updated_at"] = datetime.now(UTC).isoformat()
                changed = True
    if changed:
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)


def _desired_state(note: object) -> str:
    """Return the safe state for a note with incomplete trust evidence."""
    metadata = note.metadata
    if metadata.recommendation_state == "quarantined":
        return "quarantined"
    if metadata.superseded_by:
        return "superseded"
    if metadata.type == "workflow" and (
        metadata.confidence < 0.50 or not metadata.workflow_steps
    ):
        return "penalized"
    if metadata.schema_version < 2:
        return "penalized"
    if metadata.confidence < 0.7 or metadata.evidence_status in {
        "unknown",
        "proposal",
        "investigation",
    }:
        return "penalized"
    return "active"


def _metadata_needs_sanitization(service: BrainService, note: object) -> bool:
    """Return whether visible metadata contains unsafe text."""
    metadata = note.metadata
    if _safe_text(
        service, metadata.title
    ) != metadata.title or service.sanitizer.contains_prompt_injection(metadata.title):
        return True
    for reference in metadata.source_refs:
        if _safe_identifier_text(service, reference.id) != reference.id:
            return True
        if _safe_identifier_text(service, reference.locator) != reference.locator:
            return True
        if service.sanitizer.contains_prompt_injection(reference.locator):
            return True
    return any(
        _safe_text(service, claim.text) != claim.text
        or _safe_text(service, claim.claim_key) != claim.claim_key
        or any(
            _safe_text(service, evidence.fragment) != evidence.fragment
            for evidence in claim.evidence
        )
        for claim in metadata.claims
    )


def _sanitize_note_metadata(service: BrainService, note: object) -> bool:
    """Sanitize visible legacy metadata and flag injection-like values."""
    metadata = note.metadata
    changed = False
    title = _safe_text(service, metadata.title)
    if title != metadata.title:
        metadata.title = title
        changed = True
    injection = service.sanitizer.contains_prompt_injection(metadata.title)
    for reference in metadata.source_refs:
        safe_id = _safe_identifier_text(service, reference.id)
        safe_locator = _safe_identifier_text(service, reference.locator)
        if safe_id != reference.id:
            reference.id = safe_id
            changed = True
        if safe_locator != reference.locator:
            reference.locator = safe_locator
            changed = True
        injection = injection or service.sanitizer.contains_prompt_injection(
            reference.locator
        )
    for claim in metadata.claims:
        safe_text = _safe_text(service, claim.text)
        safe_key = _safe_text(service, claim.claim_key)
        if safe_text != claim.text:
            claim.text = safe_text
            changed = True
        if safe_key != claim.claim_key:
            claim.claim_key = safe_key
            changed = True
        for evidence in claim.evidence:
            safe_fragment = _safe_text(service, evidence.fragment)
            if safe_fragment != evidence.fragment:
                evidence.fragment = safe_fragment
                changed = True
            injection = injection or service.sanitizer.contains_prompt_injection(
                evidence.fragment
            )
    if injection:
        metadata.recommendation_state = "quarantined"
    return changed or injection


def _safe_text(service: BrainService, value: str) -> str:
    """Return one bounded, single-line sanitized metadata value."""
    return service.sanitizer.sanitize(value).text.replace("\n", " ").strip()[:1000]


def _safe_identifier_text(service: BrainService, value: str) -> str:
    """Sanitize identifiers while preserving stable opaque components."""
    return (
        service.sanitizer.sanitize_metadata(value)
        .text.replace("\n", " ")
        .strip()[:1000]
    )


def _duplicate_workflows(notes: list[object]) -> list[dict[str, str]]:
    """Find conservative duplicates among workflows for compatibility."""
    return _duplicate_notes(notes, note_types={"workflow"})


def _duplicate_notes(
    notes: list[object],
    note_types: set[str] | None = None,
) -> list[dict[str, str]]:
    """Find exact duplicates and quarantine ambiguous near-duplicates."""
    canonical: dict[str, object] = {}
    duplicates: list[dict[str, str]] = []
    candidates = sorted(
        (
            note
            for note in notes
            if (
                (note_types is None or note.metadata.type in note_types)
                and not note.metadata.superseded_by
                and note.metadata.recommendation_state != "quarantined"
                and note.content.strip()
            )
        ),
        key=lambda note: note.metadata.created_at,
    )
    for note in candidates:
        key = _duplicate_key(note)
        if key not in canonical:
            canonical[key] = note
            continue
        duplicates.append(
            {
                "duplicate_id": str(note.metadata.id),
                "canonical_id": str(canonical[key].metadata.id),
                "action": "supersede",
            }
        )
    for index, note in enumerate(candidates):
        for other in candidates[index + 1 :]:
            if _duplicate_key(note) == _duplicate_key(other):
                continue
            title_ratio = SequenceMatcher(
                None,
                _normalize_title(note.metadata.title),
                _normalize_title(other.metadata.title),
            ).ratio()
            labels_a = set(note.metadata.labels + note.metadata.manual_labels)
            labels_b = set(other.metadata.labels + other.metadata.manual_labels)
            union = labels_a | labels_b
            label_ratio = len(labels_a & labels_b) / len(union) if union else 0.0
            if (
                title_ratio >= 0.9
                and label_ratio >= 0.5
                and note.metadata.type == other.metadata.type
            ):
                older, newer = sorted(
                    (note, other), key=lambda item: item.metadata.created_at
                )
                duplicates.append(
                    {
                        "duplicate_id": str(newer.metadata.id),
                        "canonical_id": str(older.metadata.id),
                        "action": "supersede",
                        "reason": "near_identical_title_and_scope",
                    }
                )
            elif (
                0.8 <= title_ratio < 0.9
                and note.metadata.type == other.metadata.type
            ):
                duplicates.append(
                    {
                        "duplicate_id": str(other.metadata.id),
                        "canonical_id": str(note.metadata.id),
                        "action": "quarantine",
                        "reason": "ambiguous_near_duplicate",
                    }
                )
    return duplicates


def _duplicate_key(note: object) -> str:
    """Build a conservative key from type, title, scope, and content."""
    metadata = note.metadata
    normalized_content = re.sub(r"\s+", " ", note.content.lower()).strip()
    scope = metadata.scope.model_dump(mode="json")
    payload = json.dumps(
        {
            "type": metadata.type,
            "title": _normalize_title(metadata.title),
            "scope": scope,
            "content": normalized_content,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _merge_duplicate_metadata(canonical: object, duplicate: object) -> None:
    """Merge provenance and evidence before superseding a duplicate note."""
    canonical.metadata.source_refs = _unique_items(
        [*canonical.metadata.source_refs, *duplicate.metadata.source_refs],
        key=lambda item: (item.id, item.content_hash),
    )
    canonical.metadata.claims = _unique_items(
        [*canonical.metadata.claims, *duplicate.metadata.claims],
        key=lambda item: (item.claim_key, item.text, item.polarity),
    )
    canonical.metadata.links = sorted(
        set(canonical.metadata.links) | set(duplicate.metadata.links)
    )
    canonical.metadata.labels = sorted(
        set(canonical.metadata.labels) | set(duplicate.metadata.labels)
    )
    canonical.metadata.manual_labels = sorted(
        set(canonical.metadata.manual_labels) | set(duplicate.metadata.manual_labels)
    )
    canonical.metadata.confidence = max(
        canonical.metadata.confidence,
        duplicate.metadata.confidence,
    )


def _unique_items(
    items: list[object],
    key: Callable[[object], object],
) -> list[object]:
    """Deduplicate model objects while preserving first-seen order."""
    unique: list[object] = []
    seen: set[object] = set()
    for item in items:
        item_key = key(item)
        if item_key in seen:
            continue
        seen.add(item_key)
        unique.append(item)
    return unique


def _normalize_title(title: str) -> str:
    """Normalize a title for conservative duplicate detection."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _create_backup(service: BrainService, report: dict[str, object]) -> Path:
    """Create a sanitized backup before any repair mutation."""
    output_dir = service.settings.data_dir / "backups"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = output_dir / f"codex-brain-repair-{timestamp}.tar.gz"
    suffix = 1
    while backup.exists():
        backup = output_dir / (f"codex-brain-repair-{timestamp}-{suffix}.tar.gz")
        suffix += 1
    with tarfile.open(backup, "w:gz") as archive:
        for directory in (service.settings.vault_dir, service.settings.sanitized_dir):
            if directory.exists():
                archive.add(
                    directory,
                    arcname=str(directory.relative_to(service.settings.data_dir)),
                )
    return backup


def _vault_hashes(service: BrainService) -> dict[str, str]:
    """Hash canonical Markdown files for the repair manifest."""
    hashes: dict[str, str] = {}
    for root in (service.settings.vault_dir, service.settings.sanitized_dir):
        for path in root.rglob("*.md"):
            hashes[str(path.relative_to(service.settings.data_dir))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def _read_checkpoint(path: Path) -> dict[str, object]:
    """Read a non-sensitive backfill checkpoint."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"completed": []}
    return payload if isinstance(payload, dict) else {"completed": []}


def _parse_date(value: object) -> date | None:
    """Parse an optional source date from sanitized frontmatter."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _find_raw_record(service: BrainService, locator: str) -> SourceRecord | None:
    """Recover one matching read-only session segment when it is mounted."""
    if not locator.startswith("codex-session://"):
        return None
    try:
        adapter = CodexSessionAdapter(
            service.settings.codex_sessions_dir,
            service.settings.default_space,
            closed_after_seconds=service.settings.session_closed_after_seconds,
        )
        base_locator = locator.split("#", 1)[0]
        for record in adapter.records():
            if (
                record.locator == locator
                or record.locator.split("#", 1)[0] == base_locator
            ):
                return record
    except OSError:
        return None
    return None


def _retain_backups(directory: Path, keep: int) -> None:
    """Keep the newest bounded number of repair backups."""
    backups = sorted(
        directory.glob("codex-brain-repair-*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
    )
    for path in backups[:-keep]:
        path.unlink(missing_ok=True)
        path.with_suffix(".manifest.json").unlink(missing_ok=True)
