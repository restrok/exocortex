"""Application service coordinating Vault, Neo4j, gateway, and review flows."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from exocortex.actions import canonicalize_action_key
from exocortex.antigravity_sessions import (
    AntigravitySessionAdapter,
    parse_antigravity_records,
)
from exocortex.codex_sessions import CodexSessionAdapter
from exocortex.config import Settings
from exocortex.gateway import REFLECTION_PROMPT_VERSION, GatewayClient
from exocortex.graph import Neo4jStore
from exocortex.ingest import (
    TRIVIAL_FILTER_VERSION,
    Ingestor,
    IngestResult,
    SourceRecord,
)
from exocortex.labels import LabelRegistry
from exocortex.models import (
    ActionSignature,
    AnswerMode,
    Claim,
    ClaimSupport,
    FeedbackOutcome,
    KnowledgeScope,
    NoteMetadata,
    OperationalContext,
    ReflectionKnowledge,
    ResponseEnvelope,
    SearchFeedback,
    SearchResult,
    SourceReference,
    VaultNote,
    WorkflowProposal,
)
from exocortex.sanitize import Sanitizer
from exocortex.telemetry import (
    configure_telemetry,
    operation_span,
    record_ingest_summary,
    record_reflection,
    record_sync,
    traced,
)
from exocortex.vault import Vault

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthReport:
    """Status report designed for CLI and MCP output."""

    vault: str
    gateway: str
    neo4j: str
    detail: dict[str, str]


@dataclass(frozen=True)
class QueryAnalysis:
    """Deterministic search intent and entity signals."""

    text: str
    tokens: frozenset[str]
    actions: frozenset[str]
    objects: frozenset[str]
    modifiers: frozenset[str]
    entities: frozenset[str]
    phrases: tuple[str, ...]
    mode: str
    scope: KnowledgeScope


class BrainService:
    """Expose high-level operations used by both the CLI and MCP server."""

    def __init__(self, settings: Settings) -> None:
        """Build service dependencies from runtime configuration."""
        self.settings = settings
        self.settings.prepare_directories()
        configure_telemetry(settings)
        self.vault = Vault(settings.vault_dir)
        self.sanitizer = Sanitizer()
        self.gateway = GatewayClient(settings)
        self.labels = LabelRegistry(settings.state_dir / "taxonomy.json")
        self._last_ingest_summary: dict[str, object] = {}

    def initialize(self) -> None:
        """Create durable directories and Neo4j schema."""
        self.vault.ensure_exists()
        store = self._graph_store()
        try:
            store.ensure_schema()
        finally:
            store.close()

    @traced("exocortex.doctor")
    def doctor(self) -> HealthReport:
        """Check independent components without failing the complete report."""
        detail: dict[str, str] = {}
        vault_status = "ok" if self.settings.vault_dir.exists() else "missing"

        try:
            self.gateway.health()
            gateway_status = "ok"
        except Exception as error:  # pylint: disable=broad-except
            gateway_status = "unavailable"
            detail["gateway"] = _safe_error(error)

        try:
            store = self._graph_store()
            try:
                store.verify_connectivity()
            finally:
                store.close()
            neo4j_status = "ok"
        except Exception as error:  # pylint: disable=broad-except
            neo4j_status = "unavailable"
            detail["neo4j"] = _safe_error(error)

        return HealthReport(
            vault=vault_status,
            gateway=gateway_status,
            neo4j=neo4j_status,
            detail=detail,
        )

    @traced("exocortex.extraction_canary")
    def extraction_canary(self) -> dict[str, object]:
        """Validate one synthetic batch without persisting model output."""
        source_id = "codex-brain-canary"
        extracted = self.gateway.extract_batch(
            [
                {
                    "source_id": source_id,
                    "title": "Codex Brain extraction canary",
                    "event_start": "1",
                    "event_end": "1",
                    "content": (
                        "A deployment validation command completed successfully."
                    ),
                }
            ],
            timeout_seconds=self.settings.canary_timeout_seconds,
        )
        return {
            "status": "passed",
            "expected_items": 1,
            "validated_items": len(extracted),
            "source_ids_match": set(extracted) == {source_id},
        }

    def ingest(
        self,
        record: SourceRecord,
        extract: bool = True,
    ) -> IngestResult:
        """Ingest one raw source through deterministic sanitization."""
        return self._ingestor().ingest(record, extract=extract)

    @traced("exocortex.ingest_codex")
    def ingest_codex(
        self,
        sessions_root: Path | None = None,
        extract: bool = True,
        only_closed: bool = False,
        space_id: str | None = None,
        max_llm_calls: int | None = None,
        max_seconds: int | None = None,
        batch_size: int | None = None,
    ) -> list[IngestResult]:
        """Ingest Codex experiences with resumable, bounded extraction."""
        root = Path(sessions_root or self.settings.codex_sessions_dir)
        adapter = CodexSessionAdapter(
            root,
            space_id or self.settings.default_space,
            closed_after_seconds=self.settings.session_closed_after_seconds,
        )
        return self._ingest_sessions(
            adapter,
            root,
            self._load_codex_ingest_checkpoint,
            self._persist_codex_ingest_progress,
            extract=extract,
            only_closed=only_closed,
            max_llm_calls=max_llm_calls,
            max_seconds=max_seconds,
            batch_size=batch_size,
        )

    @traced("exocortex.ingest_antigravity")
    def ingest_antigravity_transcript(
        self,
        transcript_jsonl: str,
        conversation_id: str,
        space_id: str,
    ) -> ResponseEnvelope:
        """Ingest transcript JSONL content directly sent via MCP."""
        records = parse_antigravity_records(
            transcript_jsonl.splitlines(),
            conversation_id=conversation_id,
            space_id=space_id,
        )
        if not records:
            return ResponseEnvelope(
                status="ok",
                method="ingest_antigravity_transcript",
                data={
                    "conversation_id": conversation_id,
                    "records_processed": 0,
                    "status": "empty",
                },
            )
        results = self._ingestor().ingest_batch(records, extract=True)
        return ResponseEnvelope(
            status="ok",
            method="ingest_antigravity_transcript",
            data={
                "conversation_id": conversation_id,
                "records_processed": len(results),
                "notes_created": [r.note_id for r in results if r.note_id],
            },
        )

    def ingest_antigravity(
        self,
        transcripts_root: Path | None = None,
        extract: bool = True,
        only_closed: bool = False,
        space_id: str | None = None,
        max_llm_calls: int | None = None,
        max_seconds: int | None = None,
        batch_size: int | None = None,
    ) -> list[IngestResult]:
        """Ingest Antigravity transcripts with resumable, bounded extraction."""
        root = Path(
            transcripts_root
            or (Path.home() / ".gemini" / "antigravity" / "brain")
        )
        adapter = AntigravitySessionAdapter(
            root,
            space_id or self.settings.default_space,
            closed_after_seconds=self.settings.session_closed_after_seconds,
        )
        return self._ingest_sessions(
            adapter,
            root,
            self._load_antigravity_ingest_checkpoint,
            self._persist_antigravity_ingest_progress,
            extract=extract,
            only_closed=only_closed,
            max_llm_calls=max_llm_calls,
            max_seconds=max_seconds,
            batch_size=batch_size,
        )

    def _ingest_sessions(
        self,
        adapter: object,
        root: Path,
        checkpoint_loader: object,
        checkpoint_saver: object,
        extract: bool = True,
        only_closed: bool = False,
        max_llm_calls: int | None = None,
        max_seconds: int | None = None,
        batch_size: int | None = None,
    ) -> list[IngestResult]:
        """Ingest session records with resumable, bounded extraction."""
        max_llm_calls = (
            self.settings.ingest_max_llm_calls
            if max_llm_calls is None
            else max_llm_calls
        )
        max_seconds = (
            self.settings.ingest_max_seconds if max_seconds is None else max_seconds
        )
        batch_size = (
            self.settings.ingest_batch_size if batch_size is None else batch_size
        )
        if max_llm_calls < 0 or max_seconds < 0 or batch_size < 1:
            raise ValueError(
                "Ingestion limits must be non-negative and batch size positive."
            )
        paths = adapter.session_paths(only_closed=only_closed)
        checkpoint = checkpoint_loader(root)
        session_state = checkpoint["sessions"]
        started_monotonic = time.monotonic()
        started_wall = time.time()

        def elapsed_seconds() -> float:
            """Use wall time so suspend periods consume the ingest budget."""
            return max(
                time.monotonic() - started_monotonic,
                time.time() - started_wall,
            )

        with operation_span(
            "exocortex.ingest.vault_index",
            {"brain.ingest.sessions_total": len(paths)},
        ):
            ingestor = self._ingestor()
        pending_paths: list[Path] = []
        skipped_sessions = 0
        for path in paths:
            key = _session_checkpoint_key(root, path)
            signature = _session_signature(path)
            entry = session_state.get(key)
            if _checkpoint_matches(entry, signature):
                skipped_sessions += 1
            else:
                pending_paths.append(path)

        summary: dict[str, object] = {
            "status": "running",
            "source_available": root.exists(),
            "sessions_total": len(paths),
            "sessions_pending": len(pending_paths),
            "sessions_processed": 0,
            "sessions_skipped": skipped_sessions,
            "sessions_failed": 0,
            "records_attempted": 0,
            "records_processed": 0,
            "records_failed": 0,
            "records_skipped": 0,
            "records_unchanged": 0,
            "trivial_skipped": 0,
            "promoted": 0,
            "extracted": 0,
            "already_indexed": 0,
            "fallback": 0,
            "llm_calls": 0,
            "max_llm_calls": max_llm_calls,
            "max_seconds": max_seconds,
            "batch_size": batch_size,
            "stop_reason": None,
            "started_at": datetime.now(UTC).isoformat(),
        }
        session_keys = {
            _session_checkpoint_key(root, path)
            for path in paths
        }

        def refresh_pending_sessions() -> None:
            """Keep pending-session progress aligned with fallback semantics."""
            completed = sum(
                _checkpoint_entry_is_complete(session_state.get(key))
                for key in session_keys
            )
            summary["sessions_pending"] = max(len(paths) - completed, 0)

        refresh_pending_sessions()
        checkpoint_saver(checkpoint, summary)
        _LOGGER.info(
            "Ingest started sessions_total=%d pending_sessions=%d "
            "skipped_sessions=%d only_closed=%s",
            len(paths),
            len(pending_paths),
            skipped_sessions,
            only_closed,
        )

        results: list[IngestResult] = []
        for session_number, path in enumerate(pending_paths, 1):
            key = _session_checkpoint_key(root, path)
            initial_signature = _session_signature(path)
            try:
                with operation_span(
                    "exocortex.ingest.session_read",
                    {
                        "brain.ingest.session_number": session_number,
                        "brain.ingest.session_id": hashlib.sha256(
                            key.encode("utf-8")
                        ).hexdigest()[:16],
                    },
                ):
                    records = adapter.records_for_path(path)
            except Exception as error:  # pylint: disable=broad-except
                summary["sessions_failed"] = int(summary["sessions_failed"]) + 1
                _LOGGER.error(
                    "Ingest session failed session=%d/%d path=%s error=%s",
                    session_number,
                    len(pending_paths),
                    key,
                    _safe_error(error),
                )
                session_state[key] = {
                    **(_session_signature(path) or {}),
                    "records": {},
                    "status": "failed",
                    "lifecycle_state": "failed",
                    "error": _safe_error(error),
                    "failed_at": datetime.now(UTC).isoformat(),
                }
                refresh_pending_sessions()
                checkpoint_saver(checkpoint, summary)
                continue

            _LOGGER.info(
                "Ingest session started session=%d/%d path=%s records=%d "
                "pending_sessions=%d",
                session_number,
                len(pending_paths),
                key,
                len(records),
                int(summary["sessions_pending"]),
            )
            session_failed = False
            stop_requested = False
            record_state = _checkpoint_record_state(session_state.get(key))
            batch: list[tuple[int, SourceRecord, str, bool]] = []

            def persist_partial(
                current_path: Path = path,
                current_key: str = key,
                current_record_state: dict[str, dict[str, object]] = record_state,
                current_session_number: int = session_number,
                current_records_count: int = len(records),
            ) -> None:
                """Persist segment progress so interruption loses no work."""
                signature = _session_signature(current_path)
                session_state[current_key] = {
                    **(signature or {}),
                    "records": current_record_state,
                    "status": "partial",
                    "lifecycle_state": "pending",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                refresh_pending_sessions()
                with operation_span(
                    "exocortex.ingest.checkpoint",
                    {
                        "brain.ingest.session_number": current_session_number,
                        "brain.ingest.records_in_session": current_records_count,
                        "brain.ingest.records_checkpointed": len(
                            current_record_state
                        ),
                        "brain.ingest.lifecycle_state": "pending",
                    },
                ):
                    checkpoint_saver(checkpoint, summary)

            def process_batch(
                items: list[tuple[int, SourceRecord, str, bool]],
                current_session_number: int = session_number,
                current_records: list[SourceRecord] = records,
                current_record_state: dict[str, dict[str, object]] = record_state,
            ) -> None:
                """Process one bounded extraction batch and checkpoint each item."""
                nonlocal session_failed, stop_requested
                if not items:
                    return
                extraction_candidates = sum(
                    needs_call for _, _, _, needs_call in items
                )
                if extraction_candidates:
                    summary["llm_calls"] = int(summary["llm_calls"]) + 1
                elapsed = elapsed_seconds()
                remaining_seconds = (
                    max_seconds - elapsed if max_seconds else None
                )
                if extraction_candidates and remaining_seconds is not None:
                    if remaining_seconds <= 0:
                        summary["stop_reason"] = "max_seconds_reached"
                        stop_requested = True
                        return
                    request_timeout = min(
                        float(self.settings.llm_timeout_seconds),
                        float(self.settings.gateway_wall_timeout_seconds),
                        max(1.0, remaining_seconds),
                    )
                else:
                    request_timeout = None

                attributes = {
                    "brain.ingest.session_number": current_session_number,
                    "brain.ingest.record_start": items[0][0],
                    "brain.ingest.record_end": items[-1][0],
                    "brain.ingest.batch_size": len(items),
                    "brain.ingest.expected_items": extraction_candidates,
                }
                with operation_span("exocortex.ingest.batch", attributes) as span:
                    try:
                        if extract:
                            batch_results = ingestor.ingest_batch(
                                [record for _, record, _, _ in items],
                                extract=True,
                                timeout_seconds=request_timeout,
                            )
                        else:
                            batch_results = [
                                self.ingest(record, extract=False)
                                for _, record, _, _ in items
                            ]
                    except Exception as error:  # pylint: disable=broad-except
                        session_failed = True
                        batch_results = [
                            IngestResult(
                                source_id=record.source_id,
                                status="failed",
                                error=_safe_error(error),
                                llm_called=bool(extraction_candidates),
                            )
                            for _, record, _, _ in items
                        ]
                        _LOGGER.error(
                            "Ingest batch failed session=%d/%d records=%d-%d "
                            "error=%s",
                            current_session_number,
                            len(pending_paths),
                            items[0][0],
                            items[-1][0],
                            _safe_error(error),
                        )

                    span.set_attribute(
                        "brain.ingest.returned_items",
                        len(batch_results),
                    )
                    span.set_attribute(
                        "brain.ingest.fallback_items",
                        sum(
                            result.status == "promoted_fallback"
                            for result in batch_results
                        ),
                    )

                for (_, record, content_hash, _), result in zip(
                    items,
                    batch_results,
                    strict=True,
                ):
                    results.append(result)
                    _update_ingest_summary(summary, result.status)
                    if result.status == "skipped_trivial":
                        summary["trivial_skipped"] = int(summary["trivial_skipped"]) + 1
                    current_record_state[record.source_id] = {
                        "content_hash": content_hash,
                        "filter_version": TRIVIAL_FILTER_VERSION,
                        "status": result.status,
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                persist_partial()

                _LOGGER.info(
                    "Ingest progress session=%d/%d record=%d/%d "
                    "processed=%d unchanged=%d trivial=%d failed=%d llm_calls=%d "
                    "pending_sessions=%d",
                    current_session_number,
                    len(pending_paths),
                    items[-1][0],
                    len(current_records),
                    int(summary["records_processed"]),
                    int(summary["records_unchanged"]),
                    int(summary["trivial_skipped"]),
                    int(summary["records_failed"]),
                    int(summary["llm_calls"]),
                    int(summary["sessions_pending"]),
                )

            for record_number, record in enumerate(records, 1):
                summary["records_attempted"] = int(summary["records_attempted"]) + 1
                content_hash = ingestor.content_hash(record)
                if _checkpoint_record_matches(
                    record_state.get(record.source_id),
                    content_hash,
                ):
                    summary["records_skipped"] = int(summary["records_skipped"]) + 1
                    summary["records_unchanged"] = int(summary["records_unchanged"]) + 1
                    continue

                needs_call = ingestor.requires_extraction(record, extract=extract)
                elapsed = elapsed_seconds()
                if (
                    needs_call
                    and max_llm_calls >= 0
                    and int(summary["llm_calls"]) >= max_llm_calls
                ):
                    summary["stop_reason"] = "max_llm_calls_reached"
                    stop_requested = True
                    break
                if max_seconds and elapsed >= max_seconds:
                    summary["stop_reason"] = "max_seconds_reached"
                    stop_requested = True
                    break

                batch.append((record_number, record, content_hash, needs_call))
                if len(batch) >= batch_size:
                    process_batch(batch)
                    batch = []
            if batch and not stop_requested:
                process_batch(batch)

            if stop_requested:
                persist_partial()

            current_signature = _session_signature(path)
            if (
                not session_failed
                and not stop_requested
                and current_signature is not None
                and current_signature == initial_signature
            ):
                session_state[key] = {
                    **current_signature,
                    "records": record_state,
                    "status": "completed",
                    "lifecycle_state": "ingested",
                    "completed_at": datetime.now(UTC).isoformat(),
                }
                summary["sessions_processed"] = int(summary["sessions_processed"]) + 1
                summary["sessions_pending"] = int(summary["sessions_pending"]) - 1
            else:
                if session_failed:
                    has_successful_segment = any(
                        state.get("status") != "failed"
                        for state in record_state.values()
                    )
                    if has_successful_segment:
                        persist_partial()
                    else:
                        session_state[key] = {
                            **(current_signature or {}),
                            "records": record_state,
                            "status": "failed",
                            "lifecycle_state": "failed",
                            "failed_at": datetime.now(UTC).isoformat(),
                        }
                    summary["sessions_failed"] = int(summary["sessions_failed"]) + 1
                else:
                    persist_partial()
            refresh_pending_sessions()
            checkpoint_saver(checkpoint, summary)
            _LOGGER.info(
                "Ingest session checkpointed session=%d/%d path=%s records=%d "
                "processed=%d failed=%d fallback=%d pending_sessions=%d",
                session_number,
                len(pending_paths),
                key,
                len(records),
                int(summary["records_processed"]),
                int(summary["records_failed"]),
                int(summary["fallback"]),
                int(summary["sessions_pending"]),
            )
            if stop_requested:
                break

        if not bool(summary["source_available"]):
            summary["status"] = "not_available"
            summary["stop_reason"] = "sessions_source_not_available"
        elif summary["stop_reason"] or int(summary["sessions_pending"]) > 0:
            summary["status"] = "partial"
        elif int(summary["records_failed"]) or int(summary["fallback"]):
            summary["status"] = "degraded"
        else:
            summary["status"] = "completed"
        summary["completed_at"] = datetime.now(UTC).isoformat()
        self._last_ingest_summary = dict(summary)
        record_ingest_summary(summary)
        checkpoint_saver(checkpoint, summary)
        _LOGGER.info(
            "Ingest finished status=%s processed_sessions=%d skipped_sessions=%d "
            "records_processed=%d records_failed=%d pending_sessions=%d",
            summary["status"],
            summary["sessions_processed"],
            summary["sessions_skipped"],
            summary["records_processed"],
            summary["records_failed"],
            summary["sessions_pending"],
        )
        self._supersede_legacy_session_notes()
        return results

    def ingest_status(self) -> dict[str, object]:
        """Return current Codex ingestion progress without calling the gateway."""
        root = Path(self.settings.codex_sessions_dir)
        adapter = CodexSessionAdapter(
            root,
            self.settings.default_space,
            closed_after_seconds=self.settings.session_closed_after_seconds,
        )
        paths = adapter.session_paths(only_closed=True)
        checkpoint = self._load_codex_ingest_checkpoint(root)
        sessions = checkpoint["sessions"]
        last_run = checkpoint.get("last_run")
        if not isinstance(last_run, dict):
            last_run = self._last_ingest_summary
        if paths:
            sessions_total = len(paths)
            completed = sum(
                _checkpoint_matches(
                    sessions.get(_session_checkpoint_key(root, path)),
                    _session_signature(path),
                )
                for path in paths
            )
            sessions_pending = sessions_total - completed
        elif last_run:
            sessions_total = int(last_run.get("sessions_total", len(sessions)))
            completed = min(
                sum(
                    _checkpoint_entry_is_complete(entry)
                    for entry in sessions.values()
                ),
                sessions_total,
            )
            sessions_pending = max(sessions_total - completed, 0)
        else:
            sessions_total = len(sessions)
            completed = sum(
                _checkpoint_entry_is_complete(entry) for entry in sessions.values()
            )
            sessions_pending = max(sessions_total - completed, 0)
        reported_last_run = last_run
        if isinstance(last_run, dict):
            reported_last_run = dict(last_run)
            reported_last_run["sessions_pending"] = sessions_pending
        session_state_counts = {
            "ingested": 0,
            "pending": 0,
            "failed": 0,
            "not_available": 0,
        }
        if paths:
            for path in paths:
                entry = sessions.get(_session_checkpoint_key(root, path))
                state = "ingested" if _checkpoint_matches(
                    entry, _session_signature(path)
                ) else "pending"
                if isinstance(entry, dict) and entry.get("status") == "failed":
                    state = "failed"
                session_state_counts[state] += 1
        elif sessions:
            for entry in sessions.values():
                state = (
                    "ingested"
                    if _checkpoint_entry_is_complete(entry)
                    else "pending"
                )
                if isinstance(entry, dict) and entry.get("status") == "failed":
                    state = "failed"
                session_state_counts[state] += 1
        else:
            session_state_counts["not_available"] = 1
            if root.exists():
                session_state_counts["not_available"] = 0
        accounted_sessions = sum(session_state_counts.values())
        if (
            sessions_total > accounted_sessions
            and session_state_counts["not_available"] == 0
        ):
            session_state_counts["pending"] += sessions_total - accounted_sessions
        return {
            "sessions_total": sessions_total,
            "sessions_completed": completed,
            "sessions_pending": sessions_pending,
            "source_scan": "live" if paths else "checkpoint",
            "source_available": root.exists(),
            "session_state_counts": session_state_counts,
            "last_run": reported_last_run,
        }

    def promote(self, source_id: str, index: bool = True) -> VaultNote:
        """Promote a review candidate and optionally project it immediately."""
        note = self._ingestor().promote(source_id)
        if index:
            self.index_note(note, embed=False)
        return note

    def reject(self, source_id: str) -> None:
        """Reject a review candidate."""
        self._ingestor().reject(source_id)

    def remember(self, content: str, title: str, space_id: str) -> VaultNote:
        """Store one explicit sanitized memory and return its canonical note."""
        note, _ = self._remember(content, title, space_id)
        return note

    def remember_response(
        self,
        content: str,
        title: str,
        space_id: str,
    ) -> ResponseEnvelope:
        """Store a memory and report Vault/index consistency explicitly."""
        note, index_error = self._remember(content, title, space_id)
        status = "degraded" if index_error else "stored"
        return ResponseEnvelope(
            status=status,
            method="remember",
            data={
                "note_id": str(note.metadata.id),
                "note_path": note.path,
                "index_pending": bool(index_error),
                "index_error": index_error,
                "consolidation_pending": note.metadata.type != "pattern",
                "knowledge_level": note.metadata.knowledge_level,
                "pattern_key": note.metadata.pattern_key,
                "extraction_status": note.metadata.extraction_status,
            },
        )

    def _remember(
        self,
        content: str,
        title: str,
        space_id: str,
    ) -> tuple[VaultNote, str | None]:
        """Store a memory through the shared extraction and indexing path."""
        sanitized_content = self.sanitizer.sanitize(content)
        sanitized_title = self.sanitizer.sanitize(title).text.strip()
        source_id = f"memory-{sanitized_content.source_hash[:16]}"
        result = self._ingestor().ingest(
            SourceRecord(
                source_id=source_id,
                title=sanitized_title or "Captured work memory",
                content=sanitized_content.text,
                space_id=space_id,
                locator="mcp://brain_remember",
            ),
            extract=True,
            force_reextract=True,
            allow_fallback=True,
            skip_trivial=False,
            preserve_source=True,
        )
        if not result.note_id:
            raise RuntimeError(
                f"Memory ingestion did not create a note: {result.status}."
            )
        note = self.get_note(result.note_id)
        if note is None:
            raise RuntimeError(
                f"Memory ingestion returned an unknown note: {result.note_id}."
            )
        index_error: str | None = None
        try:
            self.index_note(note, embed=False)
        except Exception as error:  # pylint: disable=broad-except
            index_error = _safe_error(error)
            _LOGGER.warning(
                "Remembered note is pending index retry note_id=%s error=%s",
                note.metadata.id,
                index_error,
            )
        return note, index_error

    @traced("exocortex.sync")
    def sync(self, embed: bool = True) -> int:
        """Project pending notes using batch embeddings and Neo4j upserts."""
        state = self._load_index_state()
        notes = list(self.vault.iter_notes())
        pending_notes = [
            note
            for note in notes
            if state.get(str(note.metadata.id))
            != _index_fingerprint(note, embed, self.settings.embedding_model)
        ]
        _LOGGER.info(
            "Sync started total=%d pending=%d embed=%s",
            len(notes),
            len(pending_notes),
            embed,
        )
        count = 0
        store = self._graph_store()
        schema_ready = False
        try:
            for offset in range(
                0,
                len(pending_notes),
                self.settings.neo4j_upsert_batch_size,
            ):
                note_batch = pending_notes[
                    offset : offset + self.settings.neo4j_upsert_batch_size
                ]
                rows: list[tuple[VaultNote, list[float] | None]] = []
                embedding_pending: set[str] = set()
                for embedding_offset in range(
                    0,
                    len(note_batch),
                    self.settings.embedding_batch_size,
                ):
                    embedding_notes = note_batch[
                        embedding_offset : embedding_offset
                        + self.settings.embedding_batch_size
                    ]
                    embeddings: list[list[float] | None]
                    if embed:
                        try:
                            embeddings = list(
                                self.gateway.embed_batch(
                                    [
                                        _embedding_document(note)
                                        for note in embedding_notes
                                    ]
                                )
                            )
                        except Exception as error:  # pylint: disable=broad-except
                            _LOGGER.warning(
                                "Sync embedding batch degraded notes=%d error=%s",
                                len(embedding_notes),
                                error.__class__.__name__,
                            )
                            embeddings = [None] * len(embedding_notes)
                            embedding_pending.update(
                                str(note.metadata.id) for note in embedding_notes
                            )
                    else:
                        embeddings = [None] * len(embedding_notes)
                    for note, embedding in zip(
                        embedding_notes,
                        embeddings,
                        strict=True,
                    ):
                        if embedding:
                            note.metadata.embedding_model = (
                                self.settings.embedding_model
                            )
                        rows.append((note, embedding))

                first_embedding = next(
                    (embedding for _, embedding in rows if embedding),
                    None,
                )
                if not schema_ready:
                    store.ensure_schema(
                        embedding_dimensions=(
                            len(first_embedding) if first_embedding else None
                        )
                    )
                    schema_ready = True
                store.upsert_notes(rows)
                for note, _ in rows:
                    note_id = str(note.metadata.id)
                    if note_id in embedding_pending:
                        state[note_id] = (
                            _index_fingerprint(
                                note,
                                False,
                                self.settings.embedding_model,
                            )
                            + "|embedding-pending"
                        )
                    else:
                        state[note_id] = _index_fingerprint(
                            note,
                            embed,
                            self.settings.embedding_model,
                        )
                count += len(rows)
                self._save_index_state(state)
                _LOGGER.info(
                    "Sync progress indexed=%d/%d batch=%d",
                    count,
                    len(pending_notes),
                    len(rows),
                )
        finally:
            store.close()
        self._save_index_state(state)
        _LOGGER.info(
            "Sync finished indexed=%d remaining=%d",
            count,
            len(notes) - count,
        )
        record_sync(count, embed)
        return count

    def rebuild(self) -> int:
        """Recreate the complete Neo4j projection from canonical Markdown."""
        notes = list(self.vault.iter_notes())
        store = self._graph_store()
        try:
            store.rebuild(notes)
        finally:
            store.close()
        self._save_index_state(
            {
                str(note.metadata.id): _index_fingerprint(
                    note,
                    False,
                    self.settings.embedding_model,
                )
                for note in notes
            }
        )
        return len(notes)

    def repair_report(
        self,
        mode: str | None = None,
        fuzzy_timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Return deterministic canonical-vault repair candidates."""
        from exocortex.maintenance import repair_report

        return repair_report(
            self,
            mode=mode,
            fuzzy_timeout_seconds=fuzzy_timeout_seconds,
        )

    def repair_apply(
        self,
        mode: str | None = None,
        fuzzy_timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Backup and apply deterministic canonical-vault repairs."""
        from exocortex.maintenance import repair_apply

        return repair_apply(
            self,
            mode=mode,
            fuzzy_timeout_seconds=fuzzy_timeout_seconds,
        )

    def repair_rollback(self, backup: Path) -> dict[str, object]:
        """Restore canonical data from an explicit repair backup."""
        from exocortex.maintenance import repair_rollback

        return repair_rollback(self, backup)

    def backfill(
        self,
        batch_size: int = 25,
        resume: bool = True,
        process_all: bool = False,
        max_failures: int = 25,
    ) -> dict[str, object]:
        """Re-extract sanitized sources with a persistent checkpoint."""
        from exocortex.maintenance import backfill_sources

        return backfill_sources(
            self,
            batch_size,
            resume,
            process_all,
            max_failures,
        )

    def retry_fallbacks(
        self,
        batch_size: int = 25,
        process_all: bool = False,
        max_failures: int = 25,
    ) -> dict[str, object]:
        """Retry only sources explicitly identified as extraction fallbacks."""
        from exocortex.maintenance import retry_fallback_sources

        return retry_fallback_sources(
            self,
            batch_size=batch_size,
            process_all=process_all,
            max_failures=max_failures,
        )

    def index_note(self, note: VaultNote, embed: bool = False) -> None:
        """Upsert one canonical note and optional embedding into Neo4j."""
        embedding = self.gateway.embed(_embedding_document(note)) if embed else None
        if embedding:
            note.metadata.embedding_model = self.settings.embedding_model
        store = self._graph_store()
        try:
            if embedding:
                store.ensure_schema(embedding_dimensions=len(embedding))
            else:
                store.ensure_schema()
            store.upsert_note(note, embedding=embedding)
        finally:
            store.close()

    @traced("exocortex.search")
    def search(
        self,
        query: str,
        space_id: str | None = None,
        limit: int = 5,
        project_id: str | None = None,
        repository_id: str | None = None,
    ) -> list[SearchResult]:
        """Retrieve grounded results using hybrid lexical and semantic ranking."""
        results, _ = self._hybrid_search(
            query,
            space_id,
            limit,
            project_id=project_id,
            repository_id=repository_id,
        )
        return results

    @traced("exocortex.search_response")
    def search_response(
        self,
        query: str,
        space_id: str | None = None,
        limit: int = 5,
        project_id: str | None = None,
        repository_id: str | None = None,
        answer_mode: AnswerMode = "conservative",
        include_candidates: bool = False,
    ) -> ResponseEnvelope:
        """Return schema-v2 search data with explicit abstention status."""
        if include_candidates:
            answer_mode = "exploration"
        if answer_mode not in {"conservative", "exploration"}:
            raise ValueError("answer_mode must be conservative or exploration.")
        analysis = _analyze_query(self.sanitizer.sanitize(query).text)
        query_labels = set(self.labels.canonicalize(list(analysis.tokens)))
        generic_query = (
            analysis.mode in {"general", "exploratory"}
            and not analysis.actions
            and not _non_empty_scope(analysis.scope)
        )
        results, degraded = self._hybrid_search(
            query,
            space_id,
            max(limit * 10, 50),
            project_id=project_id,
            repository_id=repository_id,
        )
        facts = [
            result
            for result in results
            if result.claim_support == "direct"
            and result.verification_status == "verified"
            and result.scope_match != "mismatch"
        ]
        context = []
        for result in results:
            if result.verification_status != "verified":
                continue
            if result.claim_support in {"context_only", "unknown"}:
                if not _context_result_matches_query(
                    result,
                    analysis,
                    query_labels,
                ):
                    continue
                context.append(result)
            elif (
                generic_query
                and result.scope_match != "mismatch"
                and (
                    result.knowledge_level != "pattern"
                    or not _pattern_matches_query(result, analysis, query_labels)
                )
                and _generic_result_matches_query(result, analysis, query_labels)
            ):
                context.append(result)
        contradictory = [
            result
            for result in results
            if result.claim_support == "contradictory"
            and result.verification_status == "verified"
            and (
                not generic_query
                or _generic_result_matches_query(result, analysis, query_labels)
            )
        ]
        candidates = [
            result
            for result in results
            if result.verification_status == "candidate"
        ]
        pattern_results = [
            result
            for result in results
            if result.verification_status == "verified"
            and result.knowledge_level == "pattern"
            and result.scope_match != "mismatch"
            and _pattern_matches_query(result, analysis, query_labels)
        ]
        generic_fallback_results = [
            result
            for result in results
            if result.verification_status == "verified"
            and result.knowledge_level != "pattern"
            and result.scope_match != "mismatch"
            and _generic_result_matches_query(result, analysis, query_labels)
            and _fallback_result_matches_query(result, analysis, query_labels)
        ]
        data = (
            pattern_results[:limit]
            if generic_query and pattern_results
            else generic_fallback_results[:limit]
            if generic_query
            else facts[:limit]
        )
        if (
            not generic_query
            and analysis.mode in {"general", "exploratory"}
            and not analysis.actions
            and not facts
        ):
            data = [
                result
                for result in results
                if result.verification_status == "verified"
            ][:limit]
        if data:
            status = "degraded" if degraded else "ok"
        else:
            status = "abstained"
        abstention_reason = None
        if not data and _non_empty_scope(analysis.scope) and not analysis.actions:
            abstention_reason = "no_evidence_for_scope"
        elif not facts and (
            analysis.mode in {"confirmation", "procedure"} or analysis.actions
        ):
            abstention_reason = "insufficient_direct_action_evidence"
        elif not data and generic_query:
            abstention_reason = "no_general_pattern_evidence"
        elif not data and degraded:
            abstention_reason = "insufficient_literal_evidence"
        elif not data:
            abstention_reason = "no_evidence_for_query"
        return ResponseEnvelope(
            status=status,
            method="hybrid-rrf",
            data=[result.model_dump(mode="json") for result in data],
            meta={
                "limit": limit,
                "threshold": 0.45,
                "result_count": len(data),
                "embedding_degraded": degraded,
                "answer_mode": answer_mode,
                "query_mode": analysis.mode,
                "query_scope": analysis.scope.model_dump(mode="json"),
                "abstraction_order": ["pattern", "adapter", "example", "decision"],
                "context_related": [
                    _compact_search_result(result) for result in context[:limit]
                ],
                "contradictory_results": [
                    _compact_search_result(result)
                    for result in contradictory[:limit]
                ],
                "related_candidates": (
                    [_compact_search_result(result) for result in candidates[:limit]]
                    if answer_mode == "exploration"
                    else []
                ),
                "pattern_gap": not bool(pattern_results) and generic_query,
                "fallback_abstraction": (
                    sorted(
                        {
                            result.knowledge_level
                            for result in data
                            if result.knowledge_level != "pattern"
                        }
                    )
                    if generic_query and not pattern_results
                    else []
                ),
                "abstention_reason": abstention_reason,
                "message": (
                    "Encontré evidencia contradictoria; no puedo confirmar que "
                    "la acción haya ocurrido."
                    if contradictory
                    else "Encontré contexto relacionado, pero no evidencia de "
                    "que la acción haya ocurrido."
                    if abstention_reason == "insufficient_direct_action_evidence"
                    else "No encontré un patrón general confirmado; encontré "
                    "ejemplos o adaptadores relacionados."
                    if abstention_reason == "no_general_pattern_evidence"
                    else None
                ),
            },
        )

    def _hybrid_search(
        self,
        query: str,
        space_id: str | None,
        limit: int,
        project_id: str | None = None,
        repository_id: str | None = None,
    ) -> tuple[list[SearchResult], bool]:
        """Return ranked results and whether retrieval degraded to lexical mode."""
        sanitized = self.sanitizer.sanitize(query)
        if self.sanitizer.contains_prompt_injection(query) or sanitized.findings:
            return [], False
        sanitized_query = sanitized.text
        analysis = _analyze_query(sanitized_query)
        tokens = set(_tokens(sanitized_query))
        query_labels = set(self.labels.canonicalize(list(tokens)))
        lexical_results: list[SearchResult] = []
        semantic_results: list[SearchResult] = []
        degraded = False
        try:
            with operation_span(
                "exocortex.search.lexical",
                {"brain.search.limit": 50},
            ) as span:
                store = self._graph_store()
                try:
                    graph_results = store.search_fulltext(
                        sanitized_query,
                        space_id,
                        50,
                    )
                finally:
                    store.close()
                lexical_results = graph_results
                span.set_attribute(
                    "brain.search.result_count",
                    len(lexical_results),
                )
        except Exception:  # pylint: disable=broad-except
            lexical_results = self._lexical_search(sanitized_query, space_id, 50)
            degraded = True

        literal_query = _literal_anchor_query(analysis)
        if not lexical_results or literal_query:
            vault_results = self._lexical_search(sanitized_query, space_id, 50)
            known_ids = {result.note_id for result in lexical_results}
            lexical_results.extend(
                result for result in vault_results if result.note_id not in known_ids
            )

        if literal_query:
            fallback_results = self._lexical_search(literal_query, space_id, 50)
            known_ids = {result.note_id for result in lexical_results}
            lexical_results.extend(
                result
                for result in fallback_results
                if result.note_id not in known_ids
            )

        try:
            with operation_span(
                "exocortex.search.semantic",
                {
                    "brain.search.limit": 50,
                    "brain.search.embedding_timeout_seconds": (
                        self.settings.search_embedding_timeout_seconds
                    ),
                },
            ) as span:
                query_embedding = self.gateway.embed(
                    sanitized_query,
                    timeout_seconds=self.settings.search_embedding_timeout_seconds,
                )
                store = self._graph_store()
                try:
                    semantic_results = store.search_vector(
                        query_embedding,
                        space_id,
                        50,
                    )
                finally:
                    store.close()
                span.set_attribute(
                    "brain.search.result_count",
                    len(semantic_results),
                )
        except Exception:  # pylint: disable=broad-except
            degraded = True

        if degraded:
            context_results = self._lexical_search(
                sanitized_query,
                space_id,
                50,
                context_only=True,
                query_labels=query_labels,
                analysis=analysis,
            )
            known_ids = {result.note_id for result in lexical_results}
            lexical_results.extend(
                result
                for result in context_results
                if result.note_id not in known_ids
            )
            semantic_results = []
        with operation_span(
            "exocortex.search.rank",
            {
                "brain.search.lexical_count": len(lexical_results),
                "brain.search.semantic_count": len(semantic_results),
            },
        ) as span:
            ranked = _fuse_results(
                lexical_results,
                semantic_results,
                tokens=tokens,
                labels=set(self.labels.canonicalize(list(tokens))),
            )
            span.set_attribute("brain.search.result_count", len(ranked))
        with operation_span(
            "exocortex.search.load_notes",
            {
                "brain.search.candidate_count": len(ranked),
                "brain.search.note_lookup_count": len(ranked),
            },
        ) as span:
            notes_by_id = self.vault.get_many(
                [result.note_id for result in ranked],
            )
        refreshed: list[SearchResult] = []
        incomplete_count = 0
        for result in ranked:
            note = notes_by_id.get(result.note_id)
            if note is not None:
                if not _is_queryable_note(note):
                    incomplete_count += 1
                    continue
                if not _scope_matches(
                    note,
                    analysis,
                    project_id=project_id,
                    repository_id=repository_id,
                    query_labels=query_labels,
                ):
                    continue
                result.title = note.metadata.title
                result.note_type = note.metadata.type
                result.space_id = note.metadata.space_id
                result.path = note.path
                result.excerpt = _excerpt(note.content, tokens)
                result.source_refs = note.metadata.source_refs
                result.labels = _effective_labels(note)
                result.evidence_status = note.metadata.evidence_status
                result.recommendation_state = note.metadata.recommendation_state
                result.confidence = note.metadata.confidence
                result.recommendation_level = (
                    _recommendation_level(note.metadata.confidence)
                    if note.metadata.type == "workflow"
                    else None
                )
                result.claims = note.metadata.claims
                result.actions = _note_actions(note)
                result.action_score = _action_match_score(
                    sanitized_query,
                    result.actions,
                )
                _annotate_search_result(result, note, analysis)
                _annotate_scope(result, note, analysis)
                if _entity_scope_mismatch(note, analysis):
                    result.scope_match = "mismatch"
                    result.scope_fit_score = 0.25
                result.score = round(
                    result.score
                    * (
                        0.75
                        + 0.15 * result.scope_fit_score
                        + 0.10 * result.abstraction_fit_score
                    ),
                    6,
                )
                if note.metadata.type == "workflow":
                    self._annotate_workflow_result(result, note)
            refreshed.append(result)
        span.set_attribute("brain.search.incomplete_count", incomplete_count)
        if not refreshed or refreshed[0].score < 0.45:
            return [], degraded
        return refreshed[:limit], degraded

    def get_note(self, note_id: str) -> VaultNote | None:
        """Retrieve canonical Markdown by stable note identifier."""
        return self.vault.get(note_id)

    def notes_by_date(
        self,
        start_on: date,
        end_on: date,
        space_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Return notes whose source conversations occurred within a date range."""
        if start_on > end_on:
            raise ValueError("start_on must be on or before end_on.")
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        if offset < 0:
            raise ValueError("offset must be non-negative.")

        results: list[SearchResult] = []
        for note in self.vault.iter_notes(space_id):
            if not _is_queryable_note(note):
                continue
            source_refs = _dated_source_refs(note, start_on, end_on)
            if not source_refs:
                continue
            results.append(
                SearchResult(
                    note_id=str(note.metadata.id),
                    title=note.metadata.title,
                    note_type=note.metadata.type,
                    space_id=note.metadata.space_id,
                    path=note.path,
                    score=1.0,
                    excerpt=note.content.strip()[:500],
                    source_refs=source_refs,
                    labels=_effective_labels(note),
                    evidence_status=note.metadata.evidence_status,
                    recommendation_state=note.metadata.recommendation_state,
                    confidence=note.metadata.confidence,
                    quality_penalty=1.0
                    - _quality_factor(note.metadata.recommendation_state),
                    claims=note.metadata.claims,
                )
            )
        results.sort(
            key=lambda result: (
                min(reference.occurred_on for reference in result.source_refs),
                result.note_id,
            )
        )
        return results[offset : offset + limit]

    def date_coverage(
        self,
        start_on: date,
        end_on: date,
        space_id: str | None = None,
    ) -> dict[str, int]:
        """Report temporal coverage without treating ingestion dates as evidence."""
        if start_on > end_on:
            raise ValueError("start_on must be on or before end_on.")

        notes_scanned = 0
        notes_with_source_refs = 0
        source_refs_with_dates = 0
        source_refs_in_range = 0
        notes_without_source_dates = 0
        notes_created_in_range = 0
        notes_updated_in_range = 0
        notes_ingested_in_range = 0
        notes_in_range = 0
        for note in self.vault.iter_notes(space_id):
            notes_scanned += 1
            if not _is_queryable_note(note):
                continue
            if start_on <= note.metadata.created_at.date() <= end_on:
                notes_created_in_range += 1
            if start_on <= note.metadata.updated_at.date() <= end_on:
                notes_updated_in_range += 1
            if (
                note.metadata.ingested_at is not None
                and start_on <= note.metadata.ingested_at.date() <= end_on
            ):
                notes_ingested_in_range += 1
            if not note.metadata.source_refs:
                continue
            notes_with_source_refs += 1
            dated_refs = [
                reference
                for reference in note.metadata.source_refs
                if reference.occurred_on is not None
            ]
            source_refs_with_dates += len(dated_refs)
            source_refs_in_range += sum(
                start_on <= reference.occurred_on <= end_on
                for reference in dated_refs
            )
            if _dated_source_refs(note, start_on, end_on):
                notes_in_range += 1
            if not dated_refs:
                notes_without_source_dates += 1
        return {
            "notes_scanned": notes_scanned,
            "notes_with_source_refs": notes_with_source_refs,
            "source_refs_with_dates": source_refs_with_dates,
            "source_refs_in_range": source_refs_in_range,
            "notes_without_source_dates": notes_without_source_dates,
            "notes_created_in_range": notes_created_in_range,
            "notes_updated_in_range": notes_updated_in_range,
            "notes_ingested_in_range": notes_ingested_in_range,
            "notes_in_range": notes_in_range,
        }

    def list_by_label(
        self,
        labels: list[str],
        space_id: str | None = None,
        match_all: bool = False,
        limit: int = 25,
    ) -> list[SearchResult]:
        """Return non-superseded notes matching canonical labels."""
        requested = set(self.labels.canonicalize(labels))
        if not requested:
            return []
        results: list[SearchResult] = []
        for note in self.vault.iter_notes(space_id):
            if note.metadata.superseded_by:
                continue
            note_labels = set(_effective_labels(note))
            matches = (
                requested.issubset(note_labels)
                if match_all
                else bool(requested.intersection(note_labels))
            )
            if not matches:
                continue
            results.append(_search_result(note, float(len(requested & note_labels))))
        return sorted(results, key=lambda result: result.score, reverse=True)[:limit]

    def recommend_workflows(
        self,
        task: str,
        space_id: str | None = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        """Recommend active workflows through the hybrid retrieval pipeline."""
        candidates, _ = self._hybrid_search(task, space_id, max(limit * 10, 50))
        results = [
            result
            for result in candidates
            if result.note_type == "workflow"
            and result.recommendation_state == "active"
            and result.confidence >= 0.50
            and result.claims
        ]
        for result in results:
            note = self.vault.get(result.note_id)
            if note is not None:
                result.actions = _note_actions(note)
                result.action_score = _action_match_score(task, result.actions)
                self._annotate_workflow_result(result, note)
        results.sort(
            key=lambda result: (result.recommendation_score, result.score),
            reverse=True,
        )
        results = results[:limit]
        for result in results:
            note = self.vault.get(result.note_id)
            if note is None:
                continue
            note.metadata.usage_count += 1
            self.vault.update_metadata(note)
            self._sync_projection(note)
        return results

    def _annotate_workflow_result(
        self,
        result: SearchResult,
        note: VaultNote,
    ) -> None:
        """Attach separate evidence, quality, relevance, and ranking scores."""
        assessment = _assess_workflow(note, self.settings.operational_context)
        result.evidence_score = assessment["evidence_score"]
        result.quality_score = assessment["quality_score"]
        result.relevance_score = assessment["relevance_score"]
        result.actionability_score = assessment["actionability_score"]
        result.specificity_score = assessment["specificity_score"]
        result.genericness_penalty = assessment["genericness_penalty"]
        result.actions = _note_actions(note)
        result.action_score = max(
            result.action_score,
            _action_match_score(result.title, result.actions),
        )
        if self.settings.operational_context is None:
            result.recommendation_score = round(
                result.score * 0.80 + result.action_score * 0.20
                if result.action_score
                else result.score,
                6,
            )
            result.recommendation_level = _recommendation_level(
                note.metadata.confidence
            )
            return
        result.recommendation_score = round(
            result.score * 0.60
            + result.quality_score * 0.15
            + result.relevance_score * 0.20
            + result.action_score * 0.05,
            6,
        )
        result.recommendation_level = _recommendation_level(
            result.evidence_score,
            quality=result.quality_score,
            relevance=result.relevance_score,
            actionability=result.actionability_score,
        )

    def recommend_workflow_response(
        self,
        task: str,
        space_id: str | None = None,
        limit: int = 5,
    ) -> ResponseEnvelope:
        """Return schema-v2 workflow recommendations with abstention."""
        results = self.recommend_workflows(task, space_id, limit)
        return ResponseEnvelope(
            status="ok" if results else "abstained",
            method="hybrid-rrf-workflow",
            data=[result.model_dump(mode="json") for result in results],
            meta={
                "limit": limit,
                "threshold": 0.50,
                "auto_apply_threshold": 0.80,
                "operational_context_enabled": (
                    self.settings.operational_context is not None
                ),
                "quarantine_threshold": 0.30,
                "result_count": len(results),
            },
        )

    def get_workflow(self, workflow_id: str) -> VaultNote | None:
        """Return one active workflow by stable note identifier."""
        note = self.get_note(workflow_id)
        if (
            note is None
            or note.metadata.type != "workflow"
            or note.metadata.recommendation_state != "active"
        ):
            return None
        return note

    def record_workflow_feedback(
        self,
        workflow_id: str,
        outcome: FeedbackOutcome | str,
        notes: str | None = None,
    ) -> ResponseEnvelope:
        """Record workflow feedback and persist its updated trust metadata."""
        valid_outcomes = {
            "approved",
            "rejected",
            "executed_success",
            "failed",
        }
        if outcome not in valid_outcomes:
            raise ValueError(
                "outcome must be one of: approved, rejected, executed_success, "
                "failed."
            )
        note = self.vault.get(workflow_id)
        if (
            note is None
            or note.metadata.type != "workflow"
            or note.metadata.superseded_by
        ):
            return ResponseEnvelope(
                status="not_found",
                method="workflow-feedback",
                data=None,
            )

        positive = outcome in {"approved", "executed_success"}
        note.metadata.confidence = round(
            min(1.0, note.metadata.confidence + 0.15)
            if positive
            else max(0.0, note.metadata.confidence - 0.20),
            6,
        )
        if positive:
            note.metadata.success_count += 1
        note.metadata.last_feedback_at = datetime.now(UTC)
        note.metadata.last_feedback_notes = (
            self.sanitizer.sanitize(notes).text[:1000].strip() if notes else None
        )
        note.metadata.recommendation_state = (
            "quarantined" if note.metadata.confidence < 0.30 else "active"
        )
        self.vault.update_metadata(note)
        graph_synced = self._sync_projection(note)
        return ResponseEnvelope(
            status="ok" if graph_synced else "degraded",
            method="workflow-feedback",
            data={
                "workflow_id": workflow_id,
                "outcome": outcome,
                "confidence": note.metadata.confidence,
                "usage_count": note.metadata.usage_count,
                "success_count": note.metadata.success_count,
                "recommendation_state": note.metadata.recommendation_state,
                "recommendation_level": _recommendation_level(
                    note.metadata.confidence
                ),
                "graph_synced": graph_synced,
            },
        )

    def record_search_feedback(
        self,
        query: str,
        note_ids: list[str],
        relevance: str,
        reason: str | None = None,
        space_id: str | None = None,
        tags: list[str] | None = None,
    ) -> ResponseEnvelope:
        """Persist sanitized relevance feedback for a search response."""
        if relevance not in {"relevant", "partially_relevant", "irrelevant"}:
            raise ValueError(
                "relevance must be relevant, partially_relevant, or irrelevant."
            )
        if not note_ids:
            raise ValueError("note_ids must contain at least one note identifier.")
        missing = [note_id for note_id in note_ids if self.vault.get(note_id) is None]
        if missing:
            return ResponseEnvelope(
                status="not_found",
                method="search-feedback",
                data={"missing_note_ids": missing},
            )
        allowed_tags = {
            "scope_mismatch",
            "too_specific",
            "overgeneralized",
            "wrong_provider",
            "wrong_runtime",
            "useful_example",
            "useful_pattern",
        }
        normalized_tags = [tag for tag in (tags or []) if tag in allowed_tags]
        feedback = SearchFeedback(
            query=self.sanitizer.sanitize(query).text[:2000].strip(),
            note_ids=note_ids,
            relevance=relevance,
            reason=(
                self.sanitizer.sanitize(reason).text[:1000].strip()
                if reason
                else ""
            ),
            space_id=space_id or self.settings.default_space,
            tags=normalized_tags,
        )
        feedback_path = self.settings.data_dir / "feedback" / "search.jsonl"
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        with feedback_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "recorded_at": datetime.now(UTC).isoformat(),
                        **feedback.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return ResponseEnvelope(
            status="stored",
            method="search-feedback",
            data=feedback.model_dump(mode="json"),
        )

    @traced("exocortex.reflect")
    def reflect(self, limit: int | None = None) -> dict[str, object]:
        """Consolidate changed experiences into evidence-backed workflows."""
        state = self._load_reflection_state()
        all_experiences = [
            note
            for note in self.vault.iter_notes(self.settings.default_space)
            if note.metadata.type not in {"workflow", "pattern"}
            and not note.metadata.superseded_by
        ]
        patterns = self._store_patterns(all_experiences)
        candidates = [
            note
            for note in all_experiences
            if state.get(str(note.metadata.id)) != _note_fingerprint(note)
        ]
        _LOGGER.info(
            "Reflect started total=%d pending=%d limit=%d",
            len(all_experiences),
            len(candidates),
            limit or self.settings.reflection_max_notes,
        )
        if not candidates:
            _LOGGER.info("Reflect finished status=no_changes processed=0 workflows=0")
            return {
                "status": "reflected" if patterns else "no_changes",
                "processed": 0,
                "workflows": 0,
                "patterns": patterns,
            }
        candidate_limit = limit or self.settings.reflection_max_notes
        candidates = candidates[:candidate_limit]
        semantic_scores = self._semantic_reflection_scores(
            candidates,
            all_experiences,
            context_limit=candidate_limit * 2,
        )
        reflection_context = _related_experiences(
            candidates,
            all_experiences,
            context_limit=candidate_limit * 2,
            semantic_scores=semantic_scores,
        )
        workflows = [
            note
            for note in self.vault.iter_notes(self.settings.default_space)
            if note.metadata.type == "workflow" and not note.metadata.superseded_by
        ]
        try:
            reflection = self.gateway.reflect(
                _reflection_cards(reflection_context),
                _workflow_cards(workflows),
            )
            aliases = self._store_aliases(reflection)
            accepted = self._store_workflows(reflection)
        except Exception:
            raise
        for note in candidates:
            state[str(note.metadata.id)] = _note_fingerprint(note)
        self._save_reflection_state(state)
        result = {
            "status": "reflected",
            "processed": len(candidates),
            "workflows": accepted,
            "patterns": patterns,
            "aliases": aliases,
        }
        _LOGGER.info(
            "Reflect finished status=reflected processed=%d workflows=%d aliases=%d",
            len(candidates),
            accepted,
            aliases,
        )
        record_reflection(len(candidates), accepted)
        return result

    def learning_status(self) -> dict[str, object]:
        """Return non-sensitive reflection progress and active counts."""
        state = self._load_reflection_state()
        notes = list(self.vault.iter_notes(self.settings.default_space))
        experiences = [
            note
            for note in notes
            if note.metadata.type != "workflow" and not note.metadata.superseded_by
        ]
        processed_notes = sum(
            state.get(str(note.metadata.id)) == _note_fingerprint(note)
            for note in experiences
        )
        return {
            "processed_notes": processed_notes,
            "pending_notes": len(experiences) - processed_notes,
            "reflection_state_entries": len(state),
            "active_workflows": sum(
                1
                for note in notes
                if note.metadata.type == "workflow"
                and note.metadata.recommendation_state == "active"
                and not note.metadata.superseded_by
            ),
            "ingest": self.ingest_status(),
        }

    def audit(self) -> list[str]:
        """Return persisted file paths that still contain secret-like material."""
        findings: list[str] = []
        roots = (
            self.settings.vault_dir,
            self.settings.sanitized_dir,
            self.settings.review_dir,
        )
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                content = path.read_text(encoding="utf-8")
                if self.sanitizer.audit(content):
                    findings.append(str(path))
        return findings

    def export(self, output_dir: Path | None = None) -> Path:
        """Create a compressed backup containing no raw source rollouts."""
        output_dir = output_dir or self.settings.data_dir / "backups"
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = output_dir / f"codex-brain-{timestamp}.tar.gz"
        with tarfile.open(output_path, "w:gz") as archive:
            for directory in (self.settings.vault_dir, self.settings.sanitized_dir):
                if directory.exists():
                    archive.add(
                        directory,
                        arcname=str(directory.relative_to(self.settings.data_dir)),
                    )
        return output_path

    def _ingestor(self) -> Ingestor:
        """Create a configured ingestor for the current service."""
        return Ingestor(
            vault=self.vault,
            sanitizer=self.sanitizer,
            sanitized_dir=self.settings.sanitized_dir,
            gateway=self.gateway,
            extraction_max_chars=self.settings.extraction_max_chars,
            label_registry=self.labels,
            model_version=self.settings.llm_model,
        )

    def _graph_store(self) -> Neo4jStore:
        """Create a short-lived Neo4j store."""
        return Neo4jStore(self.settings)

    def _semantic_reflection_scores(
        self,
        candidates: list[VaultNote],
        all_experiences: list[VaultNote],
        context_limit: int,
    ) -> dict[str, float]:
        """Find historical notes related to reflection candidates by embedding."""
        if not self.settings.reflection_semantic_enabled or not candidates:
            return {}
        allowed_ids = {str(note.metadata.id) for note in all_experiences}
        query_notes = candidates[: self.settings.reflection_max_notes]
        try:
            embeddings = self.gateway.embed_batch(
                [_embedding_document(note) for note in query_notes],
                timeout_seconds=self.settings.search_embedding_timeout_seconds,
            )
            store = self._graph_store()
            try:
                scores: dict[str, float] = {}
                result_limit = min(max(context_limit, 1), len(all_experiences))
                for embedding in embeddings:
                    for result in store.search_vector(
                        embedding,
                        self.settings.default_space,
                        result_limit,
                    ):
                        if result.note_id not in allowed_ids:
                            continue
                        scores[result.note_id] = max(
                            scores.get(result.note_id, 0.0),
                            result.score,
                        )
                return scores
            finally:
                store.close()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.info(
                "Reflection semantic retrieval unavailable; using metadata and "
                "lexical signals",
                exc_info=True,
            )
            return {}

    def _sync_projection(self, note: VaultNote) -> bool:
        """Best-effort projection of one already-persisted note."""
        try:
            store = self._graph_store()
            try:
                store.upsert_note(note)
            finally:
                store.close()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Workflow projection degraded note_id=%s",
                note.metadata.id,
                exc_info=True,
            )
            return False
        return True

    def _load_index_state(self) -> dict[str, str]:
        """Load non-sensitive note fingerprints for incremental projection."""
        path = self.settings.state_dir / "index-state.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_index_state(self, state: dict[str, str]) -> None:
        """Persist note fingerprints only after a successful synchronization."""
        path = self.settings.state_dir / "index-state.json"
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(state, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _load_reflection_state(self) -> dict[str, str]:
        """Load non-sensitive note fingerprints processed by reflection."""
        path = self.settings.state_dir / "reflection-state.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_reflection_state(self, state: dict[str, str]) -> None:
        """Persist reflection fingerprints only after successful consolidation."""
        path = self.settings.state_dir / "reflection-state.json"
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(state, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _load_antigravity_ingest_checkpoint(self, root: Path) -> dict[str, object]:
        """Load non-sensitive Antigravity ingestion progress."""
        path = self._antigravity_ingest_checkpoint_path(root)
        default: dict[str, object] = {
            "version": 2,
            "root_id": _sessions_root_id(root),
            "sessions": {},
        }
        if not path.is_file():
            return default
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("version", 2)
                payload.setdefault("root_id", _sessions_root_id(root))
                payload.setdefault("sessions", {})
                return payload
            return default
        except (json.JSONDecodeError, OSError):
            return default

    def _persist_antigravity_ingest_progress(
        self,
        checkpoint: dict[str, object],
        summary: dict[str, object],
    ) -> None:
        """Persist non-sensitive Antigravity ingestion state."""
        root_id = str(checkpoint.get("root_id") or "default")
        path = self.settings.state_dir / f"antigravity-ingest-checkpoint-{root_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint["last_run"] = dict(summary)
        temporary_path = path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _antigravity_ingest_checkpoint_path(self, root: Path) -> Path:
        """Return the checkpoint path for Antigravity transcripts root."""
        root_id = _sessions_root_id(root)
        return self.settings.state_dir / f"antigravity-ingest-checkpoint-{root_id}.json"

    def _load_codex_ingest_checkpoint(self, root: Path) -> dict[str, object]:
        """Load non-sensitive per-rollout ingestion progress."""
        path = self._codex_ingest_checkpoint_path(root)
        default: dict[str, object] = {
            "version": 2,
            "root_id": _sessions_root_id(root),
            "sessions": {},
        }
        if not path.exists():
            return default
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(payload, dict):
            return default
        if payload.get("root_id") != default["root_id"]:
            return default
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return default
        payload["version"] = 2
        payload["sessions"] = sessions
        return payload

    def _persist_codex_ingest_progress(
        self,
        checkpoint: dict[str, object],
        summary: dict[str, object],
    ) -> None:
        """Persist ingestion progress atomically after each completed session."""
        checkpoint["last_run"] = dict(summary)
        root_id = str(checkpoint.get("root_id") or "")
        path = self.settings.state_dir / "codex-ingest-checkpoint.json"
        default_root_id = _sessions_root_id(self.settings.codex_sessions_dir)
        if root_id and root_id != default_root_id:
            path = self.settings.state_dir / f"codex-ingest-checkpoint-{root_id}.json"
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _codex_ingest_checkpoint_path(self, root: Path) -> Path:
        """Return the checkpoint path isolated to one mounted sessions root."""
        root_id = _sessions_root_id(root)
        default_root_id = _sessions_root_id(self.settings.codex_sessions_dir)
        if root_id == default_root_id:
            return self.settings.state_dir / "codex-ingest-checkpoint.json"
        return self.settings.state_dir / f"codex-ingest-checkpoint-{root_id}.json"

    def _store_workflows(self, reflection: ReflectionKnowledge) -> int:
        """Validate evidence and persist accepted workflow proposals."""
        notes = {
            str(note.metadata.id): note
            for note in self.vault.iter_notes(self.settings.default_space)
        }
        accepted = 0
        for proposal in reflection.workflows:
            proposal_action = _proposal_action(proposal)
            evidence = [
                notes[note_id]
                for note_id in proposal.evidence_note_ids
                if note_id in notes
                and notes[note_id].metadata.type != "workflow"
                and not notes[note_id].metadata.superseded_by
            ]
            if not _valid_workflow_evidence(evidence, proposal):
                continue
            references = _unique_source_refs(evidence)
            existing_workflow, duplicate_state = _workflow_match(
                proposal,
                [
                    note
                    for note in notes.values()
                    if note.metadata.type == "workflow"
                    and not note.metadata.superseded_by
                ],
            )
            if existing_workflow is not None and duplicate_state == "strong":
                existing_workflow.metadata.source_refs = _unique_source_refs(
                    [existing_workflow, *evidence]
                )
                existing_workflow.metadata.claims = _evidence_claims(
                    [existing_workflow, *evidence]
                )
                existing_workflow.metadata.workflow_steps = [
                    step.model_dump(mode="json") for step in proposal.steps
                ]
                if proposal_action is not None:
                    existing_workflow.metadata.actions = [proposal_action]
                existing_workflow.metadata.confidence = max(
                    existing_workflow.metadata.confidence,
                    _initial_workflow_confidence(evidence, proposal),
                )
                if existing_workflow.metadata.recommendation_state != "quarantined":
                    existing_workflow.metadata.recommendation_state = "active"
                self.vault.upsert_managed(
                    existing_workflow.metadata,
                    _render_workflow(proposal),
                )
                continue
            title_hash = hashlib.sha256(proposal.title.lower().encode()).hexdigest()
            source_id = f"workflow-{title_hash[:16]}"
            metadata = NoteMetadata(
                schema_version=2,
                type="workflow",
                title=proposal.title.strip(),
                space_id=self.settings.default_space,
                ingested_at=datetime.now(UTC),
                source_refs=[],
                confidence=_initial_workflow_confidence(evidence, proposal),
                labels=self.labels.canonicalize(proposal.labels),
                evidence_status=(
                    "confirmed_success"
                    if any(
                        note.metadata.evidence_status == "confirmed_success"
                        for note in evidence
                    )
                    else "decision"
                ),
                recommendation_state=(
                    "quarantined" if duplicate_state == "ambiguous" else "active"
                ),
                prompt_version=REFLECTION_PROMPT_VERSION,
                model_version=self.settings.reflection_model,
                claims=_evidence_claims(evidence),
                actions=[proposal_action] if proposal_action is not None else [],
                workflow_steps=[
                    step.model_dump(mode="json") for step in proposal.steps
                ],
            )
            existing = self.vault.find_by_source_id(source_id)
            if existing:
                metadata.id = existing.metadata.id
                metadata.created_at = existing.metadata.created_at
                metadata.manual_labels = existing.metadata.manual_labels
            workflow_reference = SourceReference(
                id=source_id,
                locator=f"workflow://{source_id}",
                content_hash=hashlib.sha256(proposal.title.encode("utf-8")).hexdigest(),
            )
            metadata.source_refs = [workflow_reference, *references]
            content = _render_workflow(proposal)
            self.vault.upsert_managed(metadata, content)
            accepted += 1
        return accepted

    def _store_patterns(self, notes: list[VaultNote]) -> int:
        """Materialize reusable patterns from independent scoped examples."""
        grouped: dict[str, list[VaultNote]] = {}
        existing: dict[str, VaultNote] = {}
        for note in self.vault.iter_notes(self.settings.default_space):
            if note.metadata.type != "pattern" or note.metadata.superseded_by:
                continue
            key = _normalize_pattern_key(note.metadata.pattern_key)
            if key:
                existing[key] = note
        for note in notes:
            if note.metadata.knowledge_level not in {"adapter", "example"}:
                continue
            key = _normalize_pattern_key(note.metadata.pattern_key)
            if key:
                grouped.setdefault(key, []).append(note)

        stored = 0
        for pattern_key, examples in grouped.items():
            source_ids = {
                reference.id
                for note in examples
                for reference in note.metadata.source_refs
                if not reference.id.startswith("pattern-")
            }
            if len(source_ids) < 2 or pattern_key in existing:
                continue
            references = _unique_source_refs(examples)
            if len(references) < 2:
                continue
            pattern_hash = hashlib.sha256(pattern_key.encode()).hexdigest()[:16]
            source_id = f"pattern-{pattern_hash}"
            pattern_reference = SourceReference(
                id=source_id,
                locator=f"brain://pattern/{pattern_key}",
                content_hash=hashlib.sha256(
                    f"pattern:{pattern_key}".encode()
                ).hexdigest(),
            )
            confidence = min(
                0.85,
                max(
                    0.50,
                    sum(note.metadata.confidence for note in examples)
                    / len(examples),
                ),
            )
            metadata = NoteMetadata(
                schema_version=2,
                type="pattern",
                title=_pattern_title(pattern_key),
                space_id=self.settings.default_space,
                ingested_at=datetime.now(UTC),
                source_refs=[pattern_reference, *references],
                confidence=round(confidence, 6),
                evidence_status="investigation",
                recommendation_state="active",
                extraction_status="manual",
                knowledge_level="pattern",
                pattern_key=pattern_key,
            )
            note = self.vault.upsert_managed(
                metadata,
                _render_pattern(pattern_key, examples),
            )
            self._sync_projection(note)
            existing[pattern_key] = note
            stored += 1
        return stored

    def _store_aliases(self, reflection: ReflectionKnowledge) -> int:
        """Persist only high-confidence, reversible semantic aliases."""
        accepted = 0
        for proposal in reflection.aliases:
            if proposal.confidence < 0.95:
                continue
            self.labels.register_alias(proposal.alias, proposal.canonical)
            accepted += 1
        return accepted

    def _supersede_legacy_session_notes(self) -> int:
        """Hide pre-segmentation session notes after segmented evidence exists."""
        notes = list(self.vault.iter_notes())
        segmented_by_locator: dict[str, str] = {}
        for note in notes:
            for reference in note.metadata.source_refs:
                if reference.segment_id and reference.locator.startswith(
                    "codex-session://"
                ):
                    base_locator = reference.locator.split("#", 1)[0]
                    segmented_by_locator.setdefault(base_locator, str(note.metadata.id))
        superseded = 0
        for note in notes:
            if note.metadata.superseded_by:
                continue
            legacy_refs = [
                reference
                for reference in note.metadata.source_refs
                if reference.locator.startswith("codex-session://")
                and reference.segment_id is None
            ]
            replacement = next(
                (
                    segmented_by_locator.get(reference.locator)
                    for reference in legacy_refs
                    if segmented_by_locator.get(reference.locator)
                ),
                None,
            )
            if replacement and replacement != str(note.metadata.id):
                note.metadata.superseded_by = replacement
                self.vault.update_metadata(note)
                superseded += 1
        return superseded

    def _lexical_search(
        self,
        query: str,
        space_id: str | None,
        limit: int,
        context_only: bool = False,
        query_labels: set[str] | None = None,
        analysis: QueryAnalysis | None = None,
    ) -> list[SearchResult]:
        """Search Markdown locally when a graph index is unavailable.

        Context-only retrieval may use a lower lexical coverage threshold for
        reusable patterns and adapters. The later scope and evidence filters
        keep those candidates from becoming authoritative answer data.
        """
        sanitized = self.sanitizer.sanitize(query)
        if self.sanitizer.contains_prompt_injection(query) or sanitized.findings:
            return []
        tokens = _tokens(sanitized.text)
        if not tokens:
            return []
        candidates: list[SearchResult] = []
        for note in self.vault.iter_notes(space_id):
            if note.metadata.superseded_by:
                continue
            title = note.metadata.title.lower()
            content = note.content.lower()
            unique_tokens = set(tokens)
            matched_tokens = sum(
                token in title or token in content for token in unique_tokens
            )
            is_generic_knowledge = note.metadata.knowledge_level in {
                "pattern",
                "adapter",
            }
            if context_only:
                if not is_generic_knowledge or matched_tokens < 2:
                    continue
                if query_labels and not _technical_labels_overlap(
                    query_labels,
                    _effective_labels(note),
                ):
                    continue
                if (
                    matched_tokens < 4
                    and (analysis is None or not _scope_text_overlap(note, analysis))
                ):
                    continue
            else:
                minimum_coverage = 0.75 if len(unique_tokens) >= 3 else 0.5
                if matched_tokens / len(unique_tokens) < minimum_coverage:
                    continue
            title_score = sum(title.count(token) for token in tokens)
            content_score = sum(content.count(token) for token in tokens)
            score = (title_score * 5) + content_score
            if not score:
                continue
            if _tokens(note.metadata.title) == tokens:
                score += len(tokens) * 10
            candidates.append(
                SearchResult(
                    note_id=str(note.metadata.id),
                    title=note.metadata.title,
                    note_type=note.metadata.type,
                    space_id=note.metadata.space_id,
                    path=note.path,
                    score=float(score),
                    excerpt=_excerpt(note.content, set(tokens)),
                    source_refs=note.metadata.source_refs,
                    labels=_effective_labels(note),
                    evidence_status=note.metadata.evidence_status,
                    recommendation_state=note.metadata.recommendation_state,
                    confidence=note.metadata.confidence,
                    recommendation_level=(
                        _recommendation_level(note.metadata.confidence)
                        if note.metadata.type == "workflow"
                        else None
                    ),
                    claims=note.metadata.claims,
                )
            )
        return sorted(candidates, key=lambda result: result.score, reverse=True)[:limit]


def _tokens(value: str) -> list[str]:
    """Return query terms suitable for basic local lexical matching."""
    stop_words = {
        "about",
        "also",
        "como",
        "con",
        "de",
        "en",
        "for",
        "from",
        "have",
        "into",
        "para",
        "that",
        "the",
        "this",
        "una",
        "with",
    }
    return [
        token
        for token in re.findall(r"[\w-]{2,}", value.lower())
        if token not in stop_words or token.upper() in {"CI", "IAM", "IAP"}
    ]


def _expanded_search_tokens(value: str) -> set[str]:
    """Return lexical terms in both identifier and human-readable forms."""
    return set(_tokens(value)) | set(_tokens(value.replace("-", " ")))


def _literal_anchor_query(analysis: QueryAnalysis) -> str:
    """Build a small Vault fallback query from high-signal literal anchors."""
    anchors = set(analysis.entities) | set(analysis.objects)
    if not analysis.entities:
        anchors.update(analysis.modifiers)
    if not anchors:
        return ""
    terms: set[str] = set()
    for anchor in anchors:
        terms.update(anchor.replace("-", " ").split())
    return " ".join(sorted(terms))


_ACTION_WORDS = frozenset(
    {
        "add",
        "approve",
        "create",
        "delete",
        "deploy",
        "disable",
        "eliminate",
        "eliminated",
        "ejecutar",
        "execute",
        "grant",
        "remove",
        "removed",
        "run",
        "update",
        "actualizar",
        "borrar",
        "borramos",
        "borró",
        "borraron",
        "eliminar",
        "eliminamos",
        "eliminó",
        "eliminaron",
        "quitamos",
        "removimos",
    }
)
_OBJECT_WORDS = frozenset(
    {
        "branch",
        "container",
        "dataset",
        "file",
        "image",
        "repository",
        "repo",
        "resource",
        "secret",
        "table",
        "version",
        "workflow",
        "rama",
    }
)
_MODIFIER_WORDS = frozenset(
    {
        "default",
        "local",
        "source",
        "target",
        "stuck",
        "atascada",
        "seguro",
        "safe",
    }
)
_CONFIRMATION_WORDS = frozenset(
    {
        "did",
        "happened",
        "occurred",
        "occurrió",
        "ocurrio",
        "confirm",
        "confirmed",
        "was",
        "were",
        "se",
        "fue",
        "fueron",
        "eliminado",
        "eliminada",
        "deleted",
        "removed",
    }
)
_PROCEDURE_WORDS = frozenset(
    {"how", "cómo", "como", "process", "proceso", "steps", "pasos", "procedure"}
)
_EXPLORATION_WORDS = frozenset(
    {"related", "relacionado", "relacionadas", "antecedentes", "background", "similar"}
)
_SCOPE_ALIASES: dict[str, tuple[str, str]] = {
    "acme": ("organization", "acme-corp"),
    "acme-corp": ("organization", "acme-corp"),
    "acmecorp": ("organization", "acme-corp"),
    "gcp": ("provider", "gcp"),
    "aws": ("provider", "aws"),
    "azure": ("provider", "azure"),
    "cloud-run": ("runtime", "cloud-run"),
    "cloudrun": ("runtime", "cloud-run"),
    "kubernetes": ("runtime", "kubernetes"),
    "docker": ("runtime", "docker"),
    "production": ("environment", "production"),
    "prod": ("environment", "production"),
    "staging": ("environment", "staging"),
    "development": ("environment", "development"),
    "local": ("environment", "local"),
}


def _analyze_query(query: str) -> QueryAnalysis:
    """Extract deterministic intent, entities, and query mode signals."""
    normalized = " ".join(query.lower().split())
    tokens = frozenset(_tokens(normalized))
    actions = set(tokens.intersection(_ACTION_WORDS))
    if "cloud" in tokens and "run" in actions:
        actions.remove("run")
    objects = tokens.intersection(_OBJECT_WORDS)
    modifiers = tokens.intersection(_MODIFIER_WORDS)
    entities = frozenset(
        token
        for token in tokens
        if ("-" in token or "_" in token or "." in token)
        and token not in actions
    )
    if tokens.intersection(_CONFIRMATION_WORDS):
        mode = "confirmation"
    elif tokens.intersection(_PROCEDURE_WORDS):
        mode = "procedure"
    elif tokens.intersection(_EXPLORATION_WORDS):
        mode = "exploratory"
    else:
        mode = "general"
    phrases = (normalized,) if len(tokens) >= 2 else ()
    return QueryAnalysis(
        text=normalized,
        tokens=tokens,
        actions=frozenset(actions),
        objects=frozenset(objects),
        modifiers=frozenset(modifiers),
        entities=entities,
        phrases=phrases,
        mode=mode,
        scope=_query_scope(normalized, tokens),
    )


def _query_scope(query: str, tokens: frozenset[str]) -> KnowledgeScope:
    """Extract only explicit scope constraints from a user query."""
    values: dict[str, str] = {}
    for token in tokens:
        field_value = _SCOPE_ALIASES.get(token)
        if field_value is not None:
            values[field_value[0]] = field_value[1]
    if re.search(r"\bacme\s+corp\b", query):
        values["organization"] = "acme-corp"
    if re.search(r"\bcloud\s+run\b", query):
        values["runtime"] = "cloud-run"
    if re.search(r"\bapi\s*key\b|\bapi-key\b", query):
        values["auth"] = "api-key"
    if re.search(r"\bidentity\s+token\b|\bworkload\s+identity\b", query):
        values["auth"] = "identity-token"
    return KnowledgeScope(**values, confidence=0.95 if values else 0.0)


def _pattern_matches_query(
    result: SearchResult,
    analysis: QueryAnalysis,
    query_labels: set[str] | None = None,
) -> bool:
    """Require enough lexical intent overlap before promoting a pattern."""
    if query_labels and not _technical_labels_match(result, query_labels):
        return False
    query_terms = _expanded_search_tokens(analysis.text) - {"pattern"}
    if not query_terms:
        return True
    pattern_terms = _expanded_search_tokens(
        " ".join([result.title, result.excerpt, result.pattern_key])
    )
    overlap = query_terms.intersection(pattern_terms)
    domain_anchors = query_terms.intersection(
        {"llm", "gateway", "openai-compatible", "inference"}
    )
    if domain_anchors and not overlap.intersection(domain_anchors):
        return False
    return (
        query_terms.issubset(pattern_terms)
        or len(overlap) >= 2
        or len(overlap) * 5 >= len(query_terms) * 3
    )


def _generic_result_matches_query(
    result: SearchResult,
    analysis: QueryAnalysis,
    query_labels: set[str] | None = None,
) -> bool:
    """Keep generic context only when its domain intent is represented."""
    if query_labels and not _technical_labels_match(result, query_labels):
        return False
    query_terms = _expanded_search_tokens(analysis.text) - {"pattern"}
    result_terms = _expanded_search_tokens(
        " ".join(
            [result.title, result.excerpt, result.pattern_key, *result.labels]
        )
    )
    overlap = query_terms.intersection(result_terms)
    domain_anchors = query_terms.intersection(
        {"llm", "gateway", "openai-compatible", "inference"}
    )
    if len(domain_anchors) >= 2:
        if result.knowledge_level == "pattern":
            return bool(overlap.intersection(domain_anchors))
        return len(overlap.intersection(domain_anchors)) >= 2
    return len(overlap) >= min(2, len(query_terms))


def _context_result_matches_query(
    result: SearchResult,
    analysis: QueryAnalysis,
    query_labels: set[str] | None = None,
) -> bool:
    """Keep context only when it has explainable domain relevance."""
    if (
        result.scope_match == "mismatch"
        and result.knowledge_level in {"pattern", "adapter"}
        and _non_empty_scope(analysis.scope)
        and not _result_scope_text_overlap(result, analysis)
    ):
        return False
    if analysis.mode in {"general", "exploratory"}:
        return _generic_result_matches_query(result, analysis, query_labels)
    if any(
        reason.startswith("coincide por entidad") for reason in result.match_reasons
    ):
        return True
    if any(
        reason.startswith(
            (
                "coincide por acción",
                "coincide por objeto",
            )
        )
        for reason in result.match_reasons
    ) and _technical_labels_match(result, query_labels or set()):
        return True
    query_terms = _expanded_search_tokens(analysis.text)
    result_terms = _expanded_search_tokens(
        " ".join([result.title, result.excerpt, result.pattern_key, *result.labels])
    )
    return len(query_terms.intersection(result_terms)) >= 5


def _fallback_result_matches_query(
    result: SearchResult,
    analysis: QueryAnalysis,
    query_labels: set[str] | None = None,
) -> bool:
    """Require strong lexical coverage before exposing a scoped fallback."""
    if query_labels and not _technical_labels_match(result, query_labels):
        return False
    query_terms = _expanded_search_tokens(analysis.text) - {"pattern"}
    result_terms = _expanded_search_tokens(
        " ".join([result.title, result.excerpt, result.pattern_key])
    )
    overlap = query_terms.intersection(result_terms)
    return bool(query_terms) and len(overlap) * 4 > len(query_terms) * 3


def _technical_labels_match(
    result: SearchResult,
    query_labels: set[str],
) -> bool:
    """Require recognized technical query labels on generic results.

    A recognized technology label is stronger than semantic proximity. This
    keeps a Terraform query from promoting an unrelated pattern that happens
    to mention modularity, while leaving unrecognized vocabulary on the
    broader lexical path.
    """
    technical_labels = {
        label
        for label in query_labels
        if label.split(":", 1)[0] in {"technology", "provider", "runtime"}
    }
    if not technical_labels:
        return True
    result_labels = set(result.labels)
    result_terms = set(
        _tokens(" ".join([result.title, result.excerpt, result.pattern_key]))
    )
    for label in technical_labels:
        if label in result_labels:
            continue
        _, value = label.split(":", 1)
        variants = {value, value.replace("-", " ")}
        if variants.intersection(result_terms):
            continue
        return False
    return True


def _scope_matches(
    note: VaultNote,
    analysis: QueryAnalysis,
    project_id: str | None,
    repository_id: str | None,
    query_labels: set[str] | None = None,
) -> bool:
    """Apply hard scope filters while retaining compatible generic context."""
    document = " ".join(
        [
            note.metadata.title,
            note.content,
            " ".join(_effective_labels(note)),
        ]
    ).lower()
    if project_id and project_id.lower() not in document:
        return False
    if repository_id and repository_id.lower() not in document:
        return False
    if _all_entities_match(analysis, document):
        return True
    return _can_retain_generic_context(note, analysis, document, query_labels)


def _all_entities_match(analysis: QueryAnalysis, document: str) -> bool:
    """Return whether every explicit query entity appears in a note."""
    return all(_entity_matches(entity, document) for entity in analysis.entities)


def _entity_scope_mismatch(note: VaultNote, analysis: QueryAnalysis) -> bool:
    """Return whether a retained note missed an explicit query entity."""
    if not analysis.entities:
        return False
    document = " ".join(
        [
            note.metadata.title,
            note.content,
            " ".join(_effective_labels(note)),
        ]
    ).lower()
    return not _all_entities_match(analysis, document)


def _can_retain_generic_context(
    note: VaultNote,
    analysis: QueryAnalysis,
    document: str,
    query_labels: set[str] | None,
) -> bool:
    """Keep relevant patterns/adapters visible without making them authoritative.

    An explicit repository or service identifier is a hard filter for concrete
    notes. Reusable patterns and adapters are different: they can explain the
    applicable general approach even when they do not mention the target
    repository. They must still share a technical label or at least four lexical
    terms with the query to avoid reintroducing unrelated context.
    """
    if note.metadata.knowledge_level not in {"pattern", "adapter"}:
        return False
    if query_labels and not _technical_labels_overlap(
        query_labels,
        _effective_labels(note),
    ):
        return False
    query_terms = _expanded_search_tokens(analysis.text)
    note_terms = _expanded_search_tokens(document)
    lexical_overlap = query_terms.intersection(note_terms)
    scope_overlap = _scope_text_overlap(note, analysis)
    return scope_overlap or len(lexical_overlap) >= 4


def _technical_labels_overlap(
    query_labels: set[str],
    note_labels: list[str],
) -> bool:
    """Return whether query and note share a recognized technical label."""
    technical_query_labels = {
        label
        for label in query_labels
        if label.split(":", 1)[0] in {"technology", "provider", "runtime"}
    }
    if not technical_query_labels:
        return True
    return bool(technical_query_labels.intersection(note_labels))


def _scope_text_overlap(note: VaultNote, analysis: QueryAnalysis) -> bool:
    """Return whether an explicit query scope is represented by a note."""
    requested = _non_empty_scope(analysis.scope)
    if not requested:
        return False
    document = " ".join(
        [
            note.metadata.title,
            note.content,
            " ".join(_effective_labels(note)),
            " ".join(_non_empty_scope(note.metadata.scope).values()),
        ]
    ).lower()
    return sum(_entity_matches(value, document) for value in requested.values()) >= 2


def _result_scope_text_overlap(
    result: SearchResult,
    analysis: QueryAnalysis,
) -> bool:
    """Return whether a compact result shares at least two scope signals."""
    requested = _non_empty_scope(analysis.scope)
    if not requested:
        return False
    document = " ".join(
        [
            result.title,
            result.excerpt,
            " ".join(result.labels),
            " ".join(_non_empty_scope(result.scope).values()),
        ]
    ).lower()
    return sum(_entity_matches(value, document) for value in requested.values()) >= 2


def _entity_matches(entity: str, document: str) -> bool:
    """Match technical identifiers across hyphenated and spaced spellings."""
    normalized = entity.lower()
    variants = {normalized, normalized.replace("-", " ")}
    return any(variant in document for variant in variants)


def _annotate_search_result(
    result: SearchResult,
    note: VaultNote,
    analysis: QueryAnalysis,
) -> None:
    """Attach explainable retrieval and claim-support metadata."""
    document_tokens = set(_tokens(" ".join([note.metadata.title, note.content])))
    labels = set(label.lower() for label in _effective_labels(note))
    reasons: list[str] = []
    for phrase in analysis.phrases:
        if phrase in note.metadata.title.lower() or phrase in note.content.lower():
            reasons.append(f"coincide por {phrase}")
    for entity in sorted(analysis.entities):
        if entity in document_tokens or any(entity in label for label in labels):
            reasons.append(f"coincide por entidad {entity}")
    for token in sorted(analysis.actions):
        if token in document_tokens:
            reasons.append(f"coincide por acción {token}")
    for token in sorted(analysis.objects):
        if token in document_tokens:
            reasons.append(f"coincide por objeto {token}")
    for token in sorted(analysis.modifiers):
        if token in document_tokens:
            reasons.append(f"coincide por modificador {token}")

    action_texts = [
        " ".join(
            [
                action.action_key,
                action.canonical_action_key,
                *action.subjects,
                *action.objects,
                action.outcome,
            ]
        )
        for action in _note_actions(note)
    ]
    action_tokens = set(_tokens(" ".join(action_texts)))
    action_tokens.update(
        part
        for action_text in action_texts
        for part in re.findall(r"[a-z0-9]+", action_text.lower())
    )
    action_match = bool(analysis.actions.intersection(action_tokens))
    object_match = not analysis.objects or bool(
        analysis.objects.intersection(document_tokens)
    )
    evidence_backed = any(claim.evidence for claim in note.metadata.claims)
    negative = any(
        claim.polarity == "negated"
        or any(
            marker in claim.text.lower()
            for marker in ("failed", "falló", "no ocurrió", "did not", "not completed")
        )
        for claim in note.metadata.claims
    )
    confirmed_outcome = any(
        marker in action_text.lower()
        for action_text in action_texts
        for marker in (
            "confirmed_success",
            "completed",
            "deleted",
            "eliminated",
            "removed",
            "success",
        )
    )
    if negative:
        support: ClaimSupport = "contradictory"
    elif analysis.actions:
        support = (
            "direct"
            if action_match and object_match and (evidence_backed or confirmed_outcome)
            else "context_only"
            if action_match or object_match or analysis.entities.intersection(
                document_tokens
            )
            else "unknown"
        )
    else:
        support = "direct" if reasons and result.lexical_score > 0 else "unknown"

    result.claim_support = support
    result.match_reasons = reasons or ["coincidencia semántica"]
    result.verification_status = (
        "candidate"
        if result.semantic_score > 0 and result.lexical_score == 0
        else "verified"
    )
    support_factor = {
        "direct": 1.0,
        "context_only": 0.6,
        "contradictory": 0.2,
        "unknown": 0.35,
    }[support]
    candidate_factor = 0.5 if result.verification_status == "candidate" else 1.0
    result.retrieval_confidence = round(
        min(1.0, max(0.0, result.score) * support_factor * candidate_factor),
        6,
    )


def _annotate_scope(
    result: SearchResult,
    note: VaultNote,
    analysis: QueryAnalysis,
) -> None:
    """Rank explicit scope while keeping generic patterns eligible."""
    result.knowledge_level = note.metadata.knowledge_level
    result.pattern_key = note.metadata.pattern_key
    result.scope = note.metadata.scope
    requested = _non_empty_scope(analysis.scope)
    actual = _non_empty_scope(note.metadata.scope)
    if requested:
        matching_fields = [
            field
            for field, expected in requested.items()
            if actual.get(field, "") == expected
            or _scope_value_in_note(field, expected, note)
        ]
        if matching_fields:
            result.scope_match = "explicit"
            result.scope_fit_score = 1.0
        elif actual:
            result.scope_match = "mismatch"
            result.scope_fit_score = 0.25
        else:
            # An unscoped pattern remains useful for a scoped implementation.
            result.scope_match = "none"
            result.scope_fit_score = 0.85
    else:
        result.scope_match = "none"
        result.scope_fit_score = 1.0

    if requested:
        result.abstraction_fit_score = {
            "pattern": 0.92,
            "adapter": 1.0,
            "example": 0.75,
            "decision": 0.65,
        }.get(note.metadata.knowledge_level, 0.7)
    else:
        result.abstraction_fit_score = {
            "pattern": 1.0,
            "adapter": 0.82,
            "example": 0.70,
            "decision": 0.65,
        }.get(note.metadata.knowledge_level, 0.7)
    result.generalization_risk = (
        "low"
        if note.metadata.knowledge_level == "pattern" and not actual
        else "high"
        if note.metadata.knowledge_level in {"adapter", "example"}
        else "medium"
    )


def _non_empty_scope(scope: KnowledgeScope) -> dict[str, str]:
    """Return populated scope fields in a deterministic form."""
    return {
        field: _normalize_scope_value(value)
        for field, value in scope.model_dump().items()
        if field != "confidence" and isinstance(value, str) and value.strip()
    }


def _normalize_scope_value(value: str) -> str:
    """Normalize scope values while preserving their semantic identity."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return {
        "acme": "acme-corp",
        "acmecorp": "acme-corp",
        "cloudrun": "cloud-run",
        "identity-token": "identity-token",
    }.get(normalized, normalized)


def _scope_value_in_note(field: str, expected: str, note: VaultNote) -> bool:
    """Detect legacy scope labels and text without making them hard filters."""
    labels = {label.lower() for label in _effective_labels(note)}
    if f"{field}:{expected}" in labels:
        return True
    document = " ".join(
        [note.metadata.title, note.content, " ".join(_effective_labels(note))]
    ).lower()
    aliases = {
        "organization": (expected, expected.replace("-", " ")),
        "runtime": (expected, expected.replace("-", " ")),
        "provider": (expected,),
        "environment": (expected,),
        "auth": (expected, expected.replace("-", " ")),
    }
    return any(alias in document for alias in aliases.get(field, (expected,)))


def _effective_labels(note: VaultNote) -> list[str]:
    """Return generated and human labels without duplicates."""
    return sorted(set(note.metadata.labels + note.metadata.manual_labels))


def _note_actions(note: VaultNote) -> list[ActionSignature]:
    """Return normalized actions attached to a note."""
    actions: list[ActionSignature] = []
    for action in note.metadata.actions:
        actions.append(
            action.model_copy(
                update={
                    "canonical_action_key": canonicalize_action_key(action)
                }
            )
        )
    return actions


def _action_group_key(action: ActionSignature) -> str:
    """Return the canonical intent key used to relate action signatures."""
    return canonicalize_action_key(action)


def _action_match_score(query: str, actions: list[ActionSignature]) -> float:
    """Return the strongest lexical match between a query and note actions."""
    query_tokens = set(_tokens(query))
    if not query_tokens or not actions:
        return 0.0
    scores = []
    for action in actions:
        action_text = " ".join(
            [
                action.action_key.replace(".", " ").replace("_", " "),
                action.canonical_action_key.replace(".", " ").replace("_", " "),
                *action.subjects,
                *action.objects,
                *action.tools,
                action.route,
            ]
        )
        action_tokens = set(_tokens(action_text))
        scores.append(len(query_tokens & action_tokens) / len(query_tokens))
    return max(scores, default=0.0)


def _search_result(note: VaultNote, score: float) -> SearchResult:
    """Build a grounded result from one canonical note."""
    return SearchResult(
        note_id=str(note.metadata.id),
        title=note.metadata.title,
        note_type=note.metadata.type,
        space_id=note.metadata.space_id,
        path=note.path,
        score=score,
        excerpt=note.content.strip()[:500],
        source_refs=note.metadata.source_refs,
        labels=_effective_labels(note),
        evidence_status=note.metadata.evidence_status,
        recommendation_state=note.metadata.recommendation_state,
        confidence=note.metadata.confidence,
        actions=_note_actions(note),
        action_score=0.0,
        recommendation_level=(
            _recommendation_level(note.metadata.confidence)
            if note.metadata.type == "workflow"
            else None
        ),
        quality_penalty=1.0 - _quality_factor(note.metadata.recommendation_state),
        claims=note.metadata.claims,
        knowledge_level=note.metadata.knowledge_level,
        pattern_key=note.metadata.pattern_key,
        scope=note.metadata.scope,
    )


def _compact_search_result(result: SearchResult) -> dict[str, object]:
    """Return bounded metadata for non-authoritative search context."""
    return {
        "note_id": result.note_id,
        "title": result.title,
        "note_type": result.note_type,
        "path": result.path,
        "score": result.score,
        "excerpt": result.excerpt,
        "labels": result.labels,
        "claim_support": result.claim_support,
        "verification_status": result.verification_status,
        "match_reasons": result.match_reasons,
        "knowledge_level": result.knowledge_level,
        "pattern_key": result.pattern_key,
        "scope": result.scope.model_dump(mode="json"),
        "scope_match": result.scope_match,
        "generalization_risk": result.generalization_risk,
    }


def _fuse_results(
    lexical: list[SearchResult],
    semantic: list[SearchResult],
    tokens: set[str],
    labels: set[str],
) -> list[SearchResult]:
    """Fuse retrieval channels with RRF and bounded quality signals."""
    by_id: dict[str, SearchResult] = {}
    lexical_max = max((result.score for result in lexical), default=0.0) or 1.0
    semantic_max = max((result.score for result in semantic), default=0.0) or 1.0
    lexical_ranks = {result.note_id: index for index, result in enumerate(lexical, 1)}
    semantic_ranks = {result.note_id: index for index, result in enumerate(semantic, 1)}
    lexical_ids = set(lexical_ranks)
    semantic_ids = set(semantic_ranks)
    for result in [*lexical, *semantic]:
        if result.note_id not in by_id:
            current = result.model_copy(deep=True)
            current.score = 0.0
            current.lexical_score = 0.0
            current.semantic_score = 0.0
            by_id[result.note_id] = current
        current = by_id[result.note_id]
        current.lexical_score = max(
            current.lexical_score,
            result.score / lexical_max if result.note_id in lexical_ids else 0.0,
        )
        current.semantic_score = max(
            current.semantic_score,
            result.score / semantic_max if result.note_id in semantic_ids else 0.0,
        )
        current.labels_score = _label_overlap(result.labels, labels)
        current.graph_score = max(
            current.graph_score,
            min(result.graph_score / 10.0, 1.0),
        )
        current.quality_penalty = _quality_factor(current.recommendation_state)
        current.confidence = max(current.confidence, result.confidence)

    raw_rrf: dict[str, float] = {}
    for note_id in by_id:
        raw_rrf[note_id] = sum(
            1.0 / (60 + rank)
            for rank in (
                lexical_ranks.get(note_id),
                semantic_ranks.get(note_id),
            )
            if rank is not None
        )
    max_rrf = max(raw_rrf.values(), default=1.0) or 1.0
    for note_id, result in by_id.items():
        result.rrf_score = raw_rrf[note_id] / max_rrf
        available = [
            (0.45, result.rrf_score),
            (0.20, result.lexical_score),
            (0.20, result.semantic_score),
            (0.10, result.labels_score),
            (0.05, result.graph_score),
        ]
        active_channels = [item for item in available if item[1] > 0]
        denominator = sum(weight for weight, _ in active_channels) or 1.0
        base_score = sum(weight * value for weight, value in active_channels)
        result.score = base_score / denominator * result.quality_penalty
        result.quality_penalty = 1.0 - result.quality_penalty
        result.excerpt = _excerpt(result.excerpt, tokens)
    return sorted(by_id.values(), key=lambda result: result.score, reverse=True)


def _label_overlap(note_labels: list[str], requested: set[str]) -> float:
    """Return normalized overlap between query labels and note labels."""
    if not requested:
        return 0.0
    return len(set(note_labels).intersection(requested)) / len(requested)


def _quality_factor(state: str) -> float:
    """Return the ranking factor associated with recommendation state."""
    return {
        "active": 1.0,
        "penalized": 0.65,
        "quarantined": 0.35,
        "superseded": 0.0,
    }.get(state, 0.55)


def _embedding_document(note: VaultNote) -> str:
    """Build the sanitized search document used for semantic indexing."""
    claims = "\n".join(claim.text for claim in note.metadata.claims)
    actions = "\n".join(
        " ".join(
            [
                action.action_key,
                action.canonical_action_key,
                *action.subjects,
                *action.objects,
                *action.tools,
                action.route,
            ]
        )
        for action in _note_actions(note)
    )
    scope = " ".join(
        f"{field}:{value}"
        for field, value in note.metadata.scope.model_dump().items()
        if field != "confidence" and value
    )
    return "\n".join(
        [
            note.metadata.title,
            note.metadata.knowledge_level,
            note.metadata.pattern_key,
            scope,
            note.content,
            claims,
            actions,
        ]
    ).strip()


def _reflection_cards(notes: list[VaultNote]) -> str:
    """Render bounded, already-sanitized evidence for the reflection model."""
    grouped_cards: list[str] = []
    for canonical_action_key, grouped_notes in _reflection_action_groups(notes):
        cards: list[str] = []
        for note in grouped_notes:
            source_ids = ",".join(
                reference.id for reference in note.metadata.source_refs
            )
            cards.append(
                "\n".join(
                    [
                        f"CANONICAL_ACTION_GROUP: {canonical_action_key}",
                        f"NOTE_ID: {note.metadata.id}",
                        f"TITLE: {note.metadata.title}",
                        f"LABELS: {', '.join(_effective_labels(note))}",
                        f"EVIDENCE_STATUS: {note.metadata.evidence_status}",
                        f"RECOMMENDATION_STATE: {note.metadata.recommendation_state}",
                        f"SOURCE_IDS: {source_ids}",
                        "ACTIONS:\n"
                        + json.dumps(
                            [
                                action.model_dump(mode="json")
                                for action in _note_actions(note)
                            ],
                            sort_keys=True,
                        ),
                        "CLAIMS:\n"
                        + json.dumps(
                            [
                                claim.model_dump(mode="json")
                                for claim in _claims_by_evidence_priority(note)
                            ],
                            sort_keys=True,
                        ),
                        f"CONTENT:\n{note.content.strip()[:1200]}",
                    ]
                )
            )
        grouped_cards.append(
            f"CANONICAL_ACTION_GROUP: {canonical_action_key}\n"
            + "\n\n---\n\n".join(cards)
        )
    return "\n\n=== CANONICAL ACTION GROUP ===\n\n".join(grouped_cards)


def _claims_by_evidence_priority(note: VaultNote) -> list[Claim]:
    """Order explicit decisions above observations and model suggestions."""
    priority = {
        "user_decision": 0,
        "tool_observation": 1,
        "brain_derived": 2,
        "assistant_suggestion": 3,
    }
    return sorted(
        note.metadata.claims,
        key=lambda claim: (priority.get(claim.claim_type, 99), claim.id),
    )


def _reflection_action_groups(
    notes: list[VaultNote],
) -> list[tuple[str, list[VaultNote]]]:
    """Group reflection evidence by canonical action while retaining unkeyed notes."""
    groups: dict[str, list[VaultNote]] = {}
    for note in notes:
        action_keys = sorted(
            {
                _action_group_key(action)
                for action in _note_actions(note)
                if action.action_key.strip()
            }
        )
        if not action_keys:
            action_keys = ["(no-canonical-action-key)"]
        for action_key in action_keys:
            group = groups.setdefault(action_key, [])
            if note not in group:
                group.append(note)
    return sorted(groups.items())


def _related_experiences(
    candidates: list[VaultNote],
    all_experiences: list[VaultNote],
    context_limit: int,
    semantic_scores: dict[str, float] | None = None,
) -> list[VaultNote]:
    """Add historical experiences matching any available relationship signal."""
    selected: list[VaultNote] = []
    selected_ids: set[str] = set()
    candidate_tokens = set(
        _tokens(
            "\n".join(f"{note.metadata.title}\n{note.content}" for note in candidates)
        )
    )
    candidate_labels = {
        label for note in candidates for label in _effective_labels(note)
    }
    candidate_claim_keys = {
        claim.claim_key
        for note in candidates
        for claim in note.metadata.claims
        if claim.claim_key
    }
    candidate_action_keys = {
        _action_group_key(action)
        for note in candidates
        for action in _note_actions(note)
        if action.action_key
    }
    candidate_action_tokens = {
        token
        for note in candidates
        for action in _note_actions(note)
        for token in _tokens(
            " ".join(
                [
                    action.action_key,
                    action.canonical_action_key,
                    *action.subjects,
                    *action.objects,
                    *action.tools,
                    action.route,
                ]
            )
        )
    }
    semantic_scores = semantic_scores or {}
    scored: list[tuple[float, VaultNote]] = []
    for note in all_experiences:
        note_id = str(note.metadata.id)
        labels = set(_effective_labels(note))
        tokens = set(_tokens(f"{note.metadata.title}\n{note.content}"))
        claim_keys = {
            claim.claim_key
            for claim in note.metadata.claims
            if claim.claim_key
        }
        note_actions = _note_actions(note)
        action_keys = {_action_group_key(action) for action in note_actions}
        action_tokens = {
            token
            for action in note_actions
            for token in _tokens(
                " ".join(
                    [
                        action.action_key,
                        action.canonical_action_key,
                        *action.subjects,
                        *action.objects,
                        *action.tools,
                        action.route,
                    ]
                )
            )
        }
        metadata_score = (
            len(labels & candidate_labels) * 4
            + len(tokens & candidate_tokens)
            + len(claim_keys & candidate_claim_keys) * 6
            + len(action_keys & candidate_action_keys) * 12
            + len(action_tokens & candidate_action_tokens) * 3
        )
        semantic_score = semantic_scores.get(note_id, 0.0)
        if metadata_score > 0 or semantic_score > 0:
            scored.append((metadata_score + semantic_score * 10, note))
    for note in candidates:
        note_id = str(note.metadata.id)
        if note_id not in selected_ids:
            selected.append(note)
            selected_ids.add(note_id)
    for score, note in sorted(scored, key=lambda item: item[0], reverse=True):
        if len(selected) >= context_limit:
            break
        note_id = str(note.metadata.id)
        if score <= 0 or note_id in selected_ids:
            continue
        selected.append(note)
        selected_ids.add(note_id)
    return selected


def _workflow_cards(notes: list[VaultNote]) -> str:
    """Render existing workflows so reflection can update instead of duplicate."""
    return "\n\n---\n\n".join(
        "\n".join(
            [
                f"WORKFLOW_ID: {note.metadata.id}",
                f"TITLE: {note.metadata.title}",
                f"LABELS: {', '.join(_effective_labels(note))}",
                "ACTIONS: "
                + json.dumps(
                    [
                        action.model_dump(mode="json")
                        for action in _note_actions(note)
                    ],
                    sort_keys=True,
                ),
                note.content[:1200],
            ]
        )
        for note in notes
    )


def _valid_workflow_evidence(
    notes: list[VaultNote],
    proposal: WorkflowProposal,
) -> bool:
    """Require independent, direct, non-contradictory evidence.

    Assistant suggestions retain their provenance. Repeated positive sessions
    make them eligible, while one concrete successful action can create a
    lower-confidence confirm-first candidate.
    """
    if proposal.confidence <= 0.0:
        return False
    if not proposal.steps:
        return False
    if len(notes) < 1:
        return False
    session_ids = _source_session_ids(notes)
    if not session_ids:
        return False
    statuses = {note.metadata.evidence_status for note in notes}
    if not statuses.intersection({"confirmed_success", "decision"}):
        return False
    positive_notes = [
        note
        for note in notes
        if note.metadata.evidence_status in {"confirmed_success", "decision"}
    ]
    repeated_positive_support = len(_source_session_ids(positive_notes)) >= 2
    claims_by_id: dict[str, list[Claim]] = {}
    eligible_claims: set[str] = set()
    for note in notes:
        for claim in note.metadata.claims:
            claims_by_id.setdefault(claim.id, []).append(claim)
            if not claim.evidence or not _claim_has_supported_precision(claim):
                continue
            if claim.claim_type in {"user_decision", "tool_observation"}:
                eligible_claims.add(claim.id)
            elif claim.claim_type == "assistant_suggestion" and (
                (
                    note in positive_notes
                    and repeated_positive_support
                )
                or _single_positive_action_support(note, proposal)
            ):
                eligible_claims.add(claim.id)
    if not eligible_claims:
        return False
    for step in proposal.steps:
        if not step.evidence_claim_ids:
            return False
        if not set(step.evidence_claim_ids).issubset(eligible_claims):
            return False
        if not any(
            claim.polarity == "affirmed"
            for claim_id in step.evidence_claim_ids
            for claim in claims_by_id.get(claim_id, [])
            if claim.id in eligible_claims
        ):
            return False
    claims = [claim for claim_list in claims_by_id.values() for claim in claim_list]
    polarities: dict[str, set[str]] = {}
    for claim in claims:
        polarities.setdefault(claim.claim_key, set()).add(claim.polarity)
    return not any(len(values) > 1 for values in polarities.values())


def _source_session_ids(notes: list[VaultNote]) -> set[str]:
    """Return independent source-session identifiers for evidence notes."""
    session_ids: set[str] = set()
    for note in notes:
        for reference in note.metadata.source_refs:
            if reference.id.startswith("workflow-"):
                continue
            if reference.session_id:
                session_ids.add(reference.session_id)
                continue
            # Older notes may lack the normalized session_id. Their locator
            # still identifies the rollout; remove the thematic segment so
            # segments from one conversation do not count as separate sessions.
            session_ids.add(reference.locator.split("#", maxsplit=1)[0])
    return session_ids


def _single_positive_action_support(
    note: VaultNote,
    proposal: WorkflowProposal,
) -> bool:
    """Allow one concrete successful action as a confirm-first candidate."""
    if note.metadata.evidence_status != "confirmed_success":
        return False
    action = _proposal_action(proposal)
    if action is None:
        return False
    for note_action in _note_actions(note):
        if _action_group_key(note_action) != _action_group_key(action):
            continue
        outcome = note_action.outcome.strip().lower()
        if not outcome:
            continue
        if any(
            marker in outcome
            for marker in ("fail", "error", "pending", "proposed", "unknown")
        ):
            continue
        return True
    return False


def _claim_has_supported_precision(claim: Claim) -> bool:
    """Return whether every claim evidence span has bounded source precision."""
    return all(
        evidence.precision in {"exact", "source"} for evidence in claim.evidence
    )


def _initial_workflow_confidence(
    notes: list[VaultNote],
    proposal: WorkflowProposal,
) -> float:
    """Return a bounded starting score for a newly reflected workflow."""
    if len(notes) == 1:
        return round(min(0.65, max(0.50, proposal.confidence)), 6)
    return proposal.confidence


def _recommendation_level(
    confidence: float,
    *,
    quality: float = 1.0,
    relevance: float = 1.0,
    actionability: float = 1.0,
) -> str:
    """Map separate trust signals to the workflow interaction mode."""
    if relevance < 0.25 or quality < 0.40:
        return "deprioritize"
    if (
        confidence >= 0.80
        and quality >= 0.65
        and relevance >= 0.45
        and actionability >= 0.55
    ):
        return "auto_apply"
    return "confirm"


def _assess_workflow(
    note: VaultNote,
    context: OperationalContext | None,
) -> dict[str, float]:
    """Score workflow quality independently from its evidence eligibility."""
    content = note.content.lower()
    steps = note.metadata.workflow_steps
    covered_steps = sum(
        bool(step.get("evidence_claim_ids"))
        for step in steps
        if isinstance(step, dict)
    )
    step_coverage = covered_steps / len(steps) if steps else 0.0
    actionability = min(
        1.0,
        (0.45 if steps else 0.0)
        + (0.25 if "## validation" in content else 0.0)
        + (0.15 if "## triggers" in content else 0.0)
        + (0.15 if "## evidence" in content else 0.0),
    )
    specificity = _workflow_specificity(note)
    quality = min(
        1.0,
        0.35 * actionability + 0.35 * step_coverage + 0.30 * specificity,
    )
    genericness_penalty = round(1.0 - specificity, 6)
    relevance = _workflow_relevance(note, context, specificity)
    return {
        "evidence_score": round(note.metadata.confidence, 6),
        "quality_score": round(quality, 6),
        "relevance_score": round(relevance, 6),
        "actionability_score": round(actionability, 6),
        "specificity_score": round(specificity, 6),
        "genericness_penalty": genericness_penalty,
    }


def _workflow_specificity(note: VaultNote) -> float:
    """Estimate how much a workflow names concrete tools or work concepts."""
    generic = {
        "add",
        "a",
        "branch",
        "change",
        "document",
        "feature",
        "it",
        "move",
        "publish",
        "repository",
        "run",
        "standardize",
        "task",
        "update",
        "validate",
        "validation",
        "work",
        "workflow",
    }
    step_text = " ".join(
        str(step.get("text", ""))
        for step in note.metadata.workflow_steps
        if isinstance(step, dict)
    )
    tokens = set(
        _tokens(
            "\n".join(
                [
                    note.metadata.title,
                    " ".join(_effective_labels(note)),
                    step_text,
                ]
            )
        )
    )
    concrete = tokens - generic
    return min(1.0, len(concrete) / 6.0)


def _workflow_relevance(
    note: VaultNote,
    context: OperationalContext | None,
    specificity: float,
) -> float:
    """Rank workflow fit to context without changing evidence eligibility."""
    if context is None:
        return 1.0
    context_tokens = set(
        _tokens(
            " ".join(
                [
                    context.role,
                    *context.domains,
                    *context.common_tasks,
                    *context.preferred_tools,
                ]
            )
        )
    )
    workflow_tokens = set(
        _tokens(
            "\n".join(
                [
                    note.metadata.title,
                    note.content,
                    " ".join(_effective_labels(note)),
                ]
            )
        )
    )
    if not context_tokens:
        return 1.0
    matches = workflow_tokens & context_tokens
    match_score = min(1.0, len(matches) / max(2.0, min(5.0, len(context_tokens))))
    low_priority_tokens = set(_tokens(" ".join(context.low_priority)))
    low_priority_overlap = len(workflow_tokens & low_priority_tokens)
    relevance = 0.65 * match_score + 0.35 * specificity
    if low_priority_overlap:
        relevance -= min(0.35, 0.15 * low_priority_overlap)
    return max(0.0, min(1.0, relevance))


def _evidence_claims(notes: list[VaultNote]) -> list[Claim]:
    """Return unique claims while preserving their evidence lineage."""
    claims: dict[str, Claim] = {}
    for note in notes:
        for claim in note.metadata.claims:
            claims.setdefault(claim.id, claim)
    return list(claims.values())


def _proposal_action(proposal: WorkflowProposal) -> ActionSignature | None:
    """Return a sanitized action signature when reflection supplied one."""
    if proposal.action is None or not proposal.action.action_key.strip():
        return None
    action = proposal.action.model_copy(deep=True)
    action.action_key = _normalize_action_text(action.action_key)
    action.canonical_action_key = canonicalize_action_key(action)
    action.subjects = sorted({_normalize_action_text(item) for item in action.subjects})
    action.objects = sorted({_normalize_action_text(item) for item in action.objects})
    action.tools = sorted({_normalize_action_text(item) for item in action.tools})
    action.route = _normalize_action_text(action.route)
    return action


def _normalize_action_text(value: str) -> str:
    """Normalize action taxonomy values without collapsing distinct tools."""
    return re.sub(r"[^a-z0-9._-]+", "_", value.lower()).strip("_-")


def _same_action_route(
    proposal: ActionSignature,
    workflow: ActionSignature,
) -> bool:
    """Return whether two signatures describe the same implementation path."""
    if _action_group_key(proposal) != _action_group_key(workflow):
        return False
    proposal_route = _normalize_action_text(proposal.route)
    workflow_route = _normalize_action_text(workflow.route)
    if proposal_route and workflow_route and proposal_route != workflow_route:
        return False
    proposal_tools = {_normalize_action_text(item) for item in proposal.tools}
    workflow_tools = {_normalize_action_text(item) for item in workflow.tools}
    if proposal_tools and workflow_tools and not proposal_tools.intersection(
        workflow_tools
    ):
        return False
    return True


def _workflow_match(
    proposal: WorkflowProposal,
    workflows: list[VaultNote],
) -> tuple[VaultNote | None, str | None]:
    """Match a proposal conservatively against existing workflow notes."""
    proposal_title = _normalize_workflow_text(proposal.title)
    proposal_labels = set(proposal.labels)
    proposal_action = _proposal_action(proposal)
    best: tuple[VaultNote | None, float, float] = (None, 0.0, 0.0)
    for workflow in workflows:
        workflow_actions = _note_actions(workflow)
        if proposal_action and workflow_actions and not any(
            _same_action_route(proposal_action, action) for action in workflow_actions
        ):
            continue
        title_ratio = SequenceMatcher(
            None,
            proposal_title,
            _normalize_workflow_text(workflow.metadata.title),
        ).ratio()
        labels = set(workflow.metadata.labels + workflow.metadata.manual_labels)
        union = proposal_labels | labels
        label_ratio = len(proposal_labels & labels) / len(union) if union else 0.0
        if title_ratio > best[1]:
            best = (workflow, title_ratio, label_ratio)
    workflow, title_ratio, label_ratio = best
    if workflow is None:
        return None, None
    if title_ratio >= 0.9 and (
        label_ratio >= 0.5
        or proposal_title == _normalize_workflow_text(workflow.metadata.title)
    ):
        return workflow, "strong"
    if 0.8 <= title_ratio < 0.9:
        return workflow, "ambiguous"
    return None, None


def _normalize_workflow_text(value: str) -> str:
    """Normalize workflow titles for conservative similarity checks."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _unique_source_refs(notes: list[VaultNote]) -> list[SourceReference]:
    """Return unique source references while preserving evidence provenance."""
    references: dict[str, SourceReference] = {}
    for note in notes:
        for reference in note.metadata.source_refs:
            if not reference.id.startswith("workflow-"):
                references[reference.id] = reference
    return list(references.values())


def _render_workflow(proposal: object) -> str:
    """Render a validated workflow proposal as an Obsidian-readable note."""
    lines = ["## Summary", proposal.summary.strip()]
    if proposal.action is not None:
        action = proposal.action
        lines.extend(
            [
                "",
                "## Action",
                f"- key: {action.action_key}",
                f"- subjects: {', '.join(action.subjects)}",
                f"- objects: {', '.join(action.objects)}",
                f"- tools: {', '.join(action.tools)}",
                f"- route: {action.route}",
            ]
        )
    if proposal.triggers:
        lines.extend(["", "## Triggers", *[f"- {item}" for item in proposal.triggers]])
    if proposal.steps:
        steps = [
            f"{index}. {item.text} (claims: {', '.join(item.evidence_claim_ids)})"
            for index, item in enumerate(proposal.steps, 1)
        ]
        lines.extend(["", "## Steps", *steps])
    if proposal.validation:
        validation = [f"- {item}" for item in proposal.validation]
        lines.extend(["", "## Validation", *validation])
    if proposal.evidence_note_ids:
        evidence = [f"- {item}" for item in proposal.evidence_note_ids]
        lines.extend(["", "## Evidence", *evidence])
    return "\n".join(lines).strip()


def _normalize_pattern_key(value: str) -> str:
    """Normalize a reusable pattern identifier without provider-specific text."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _pattern_title(pattern_key: str) -> str:
    """Return a readable title for a derived pattern note."""
    return pattern_key.replace("-", " ").replace("_", " ").title()


def _is_queryable_note(note: VaultNote) -> bool:
    """Return whether a canonical note has usable, current knowledge."""
    return not note.metadata.superseded_by and bool(note.content.strip())


def _dated_source_refs(
    note: VaultNote,
    start_on: date,
    end_on: date,
) -> list[SourceReference]:
    """Return source references in one inclusive date range."""
    return [
        reference
        for reference in note.metadata.source_refs
        if reference.occurred_on is not None
        and start_on <= reference.occurred_on <= end_on
    ]


def _render_pattern(pattern_key: str, examples: list[VaultNote]) -> str:
    """Render an agnostic pattern without copying scoped implementation claims."""
    source_titles = sorted(note.metadata.title for note in examples)
    lines = [
        "## Summary",
        (
            "Reusable pattern observed across independent scoped examples. "
            "Select a provider and runtime adapter only after resolving the "
            "target context."
        ),
        "",
        "## Pattern",
        f"- pattern_key: {pattern_key}",
        "- knowledge_level: pattern",
        "",
        "## Evidence",
        *[f"- related implementation: {title}" for title in source_titles],
        "- status: derived investigation; not an execution record",
    ]
    return "\n".join(lines).strip()


def _excerpt(content: str, tokens: set[str]) -> str:
    """Return the first relevant content line, bounded for tool responses."""
    for line in content.splitlines():
        if any(token in line.lower() for token in tokens):
            return line.strip()[:500]
    return content.strip()[:500]


def _safe_error(error: Exception) -> str:
    """Return an error class only, preventing accidental credential leakage."""
    return error.__class__.__name__


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


def _checkpoint_record_state(entry: object) -> dict[str, dict[str, object]]:
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


def _update_ingest_summary(summary: dict[str, object], status: str) -> None:
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
        "actions": [
            action.model_dump(mode="json") for action in _note_actions(note)
        ],
        "content": note.content,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _index_fingerprint(note: VaultNote, embed: bool, embedding_model: str) -> str:
    """Include the projection mode in the incremental index fingerprint."""
    mode = f"embedding:{embedding_model}" if embed else "lexical"
    return f"{_note_fingerprint(note)}|{mode}"
