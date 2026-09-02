"""Source ingestion, review staging, and canonical note promotion."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter
import httpx

from exocortex.actions import canonicalize_action_key
from exocortex.gateway import (
    EXTRACTION_PROMPT_VERSION,
    GatewayClient,
    GatewayError,
)
from exocortex.labels import LabelRegistry
from exocortex.models import (
    Claim,
    EvidenceSpan,
    ExtractedKnowledge,
    ExtractionStatus,
    NoteMetadata,
    SanitizedContent,
    SourceReference,
    VaultNote,
)
from exocortex.sanitize import Sanitizer
from exocortex.vault import Vault

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceRecord:
    """Input record accepted by the ingestion pipeline."""

    source_id: str
    title: str
    content: str
    space_id: str
    locator: str
    occurred_on: date | None = None
    session_id: str | None = None
    segment_id: str | None = None
    event_start: int | None = None
    event_end: int | None = None


@dataclass(frozen=True)
class IngestResult:
    """Outcome of processing one source record."""

    source_id: str
    status: str
    note_id: str | None = None
    review_path: str | None = None
    error: str | None = None
    llm_called: bool = False


@dataclass(frozen=True)
class _PreparedRecord:
    """Sanitized source data shared by single and batch ingestion."""

    record: SourceRecord
    sanitized: SanitizedContent
    injection_detected: bool
    source_id: str
    title: str
    locator: str
    space_id: str
    reference: SourceReference


@dataclass
class _NoteIndex:
    """In-memory source indexes for one ingestion operation."""

    by_note_id: dict[str, VaultNote]
    by_source_id: dict[str, VaultNote]
    by_content_hash: dict[str, VaultNote]

    @classmethod
    def from_vault(cls, vault: Vault) -> _NoteIndex:
        """Build source indexes with one canonical-vault scan."""
        index = cls(by_note_id={}, by_source_id={}, by_content_hash={})
        for note in vault.iter_notes():
            index.replace(note)
        return index

    def by_source(self, source_id: str) -> VaultNote | None:
        """Return the note associated with one stable source identifier."""
        return self.by_source_id.get(source_id)

    def by_hash(self, content_hash: str) -> VaultNote | None:
        """Return the first note associated with one source content hash."""
        return self.by_content_hash.get(content_hash)

    def replace(self, note: VaultNote) -> None:
        """Replace one note while removing stale source-hash mappings."""
        note_id = str(note.metadata.id)
        previous = self.by_note_id.get(note_id)
        if previous is not None:
            for reference in previous.metadata.source_refs:
                if self.by_content_hash.get(reference.content_hash) is previous:
                    self.by_content_hash.pop(reference.content_hash, None)
            for source_id, candidate in list(self.by_source_id.items()):
                if candidate is previous:
                    self.by_source_id.pop(source_id, None)

        self.by_note_id[note_id] = note
        for reference in note.metadata.source_refs:
            self.by_source_id[reference.id] = note
            self.by_content_hash.setdefault(reference.content_hash, note)


TRIVIAL_FILTER_VERSION = "trivial-v1"
FALLBACK_PROMPT_VERSION = "fallback-v1"
_TRIVIAL_ACKNOWLEDGEMENTS = frozenset(
    {
        "ok",
        "okay",
        "dale",
        "done",
        "entendido",
        "gracias",
        "hello",
        "hola",
        "listo",
        "perfect",
        "perfecto",
        "thanks",
        "thank you",
        "yes",
        "si",
        "sí",
    }
)
_TECHNICAL_SIGNAL_RE = re.compile(
    r"(?:```|https?://|(?:^|\s)(?:[$>]\s*|PS>\s*)"
    r"|\b(?:api|bug|cloud|command|commit|config|deploy|docker|error|"
    r"cambi\w*|comand\w*|configur\w*|decision\w*|deploy\w*|"
    r"despleg\w*|deployment|diagnost\w*|ejecut\w*|exception|failed|"
    r"fall\w*|gcp|gateway|git|implement\w*|ingest|infra\w*|llm|mcp|"
    r"merge|migr\w*|neo4j|pattern|permis\w*|pipeline|proced\w*|"
    r"proyect\w*|prueb\w*|pytest|repo(?:sitory)?|resultad\w*|run|"
    r"segur\w*|servici\w*|status|sync|terraform|test|timeout|traceback|"
    r"valid\w*|workflow)\b"
    r"|(?:^|[\s/(])(?:[\w.-]+/)+[\w.-]+"
    r"|\b[\w.-]+\.(?:json|jsonl|md|py|sh|sql|tf|toml|yaml|yml)\b"
    r"|[{}\[\]();=])",
    re.IGNORECASE,
)
_MESSAGE_PREFIX_RE = re.compile(r"\b(?:user|assistant)\s+event=\d+:\s*", re.I)


class Ingestor:
    """Ingest sanitized source records directly into canonical Vault notes."""

    def __init__(
        self,
        vault: Vault,
        sanitizer: Sanitizer,
        sanitized_dir: Path,
        gateway: GatewayClient | None = None,
        extraction_max_chars: int = 16000,
        label_registry: LabelRegistry | None = None,
        model_version: str = "unknown",
        note_index: _NoteIndex | None = None,
    ) -> None:
        """Configure source processing dependencies."""
        self._vault = vault
        self._sanitizer = sanitizer
        self._sanitized_dir = sanitized_dir
        self._gateway = gateway
        self._extraction_max_chars = extraction_max_chars
        self._label_registry = label_registry
        self._model_version = model_version
        self._note_index = note_index or _NoteIndex.from_vault(vault)

    def ingest(
        self,
        record: SourceRecord,
        extract: bool = True,
        force_reextract: bool = False,
        allow_fallback: bool = True,
        skip_trivial: bool = True,
        preserve_source: bool = False,
    ) -> IngestResult:
        """Sanitize one source and promote it without a manual review step."""
        prepared = self._prepare_record(record)
        note = self._current_note(prepared, force_reextract)
        if note is not None:
            self._backfill_occurred_on(note, prepared.reference)
            return IngestResult(
                source_id=prepared.source_id,
                status="already_indexed",
                note_id=str(note.metadata.id),
            )
        if skip_trivial and is_trivial_content(prepared.sanitized.text):
            return IngestResult(
                source_id=prepared.source_id,
                status="skipped_trivial",
            )

        status = "promoted"
        extraction_status = "extracted" if extract else "manual"
        try:
            knowledge = self._extract(
                record,
                prepared.title,
                prepared.sanitized.text,
                prepared.reference,
                extract,
            )
        except (GatewayError, httpx.HTTPError) as error:
            if not allow_fallback:
                raise
            _LOGGER.warning(
                "Ingest extraction fallback source=%s error=%s detail=%s",
                prepared.source_id,
                error.__class__.__name__,
                _safe_error_detail(error),
            )
            knowledge = self._fallback_knowledge(
                record,
                prepared.title,
                prepared.sanitized.text,
            )
            status = "promoted_fallback"
            extraction_status = "fallback"
        knowledge = self._sanitize_knowledge(
            knowledge,
            prepared.sanitized.text,
            prepared.reference,
        )
        if prepared.injection_detected:
            status = "promoted_quarantined"
        note = self._promote(
            prepared.space_id,
            knowledge,
            prepared.reference,
            quarantined=prepared.injection_detected,
            extraction_status=extraction_status,
            source_content=prepared.sanitized.text if preserve_source else None,
        )
        return IngestResult(
            source_id=prepared.source_id,
            status=status,
            note_id=str(note.metadata.id),
            llm_called=extract,
        )

    def ingest_batch(
        self,
        records: list[SourceRecord],
        extract: bool = True,
        force_reextract: bool = False,
        allow_fallback: bool = True,
        timeout_seconds: float | None = None,
    ) -> list[IngestResult]:
        """Ingest several records with one structured gateway request."""
        if not records:
            return []
        if not extract:
            return [
                self.ingest(
                    record,
                    extract=False,
                    force_reextract=force_reextract,
                    allow_fallback=allow_fallback,
                )
                for record in records
            ]
        if self._gateway is None:
            raise RuntimeError("A gateway is required when extraction is enabled.")

        results_by_source: dict[str, IngestResult] = {}
        candidates: list[_PreparedRecord] = []
        for record in records:
            prepared = self._prepare_record(record)
            note = self._current_note(prepared, force_reextract)
            if note is not None:
                self._backfill_occurred_on(note, prepared.reference)
                results_by_source[prepared.source_id] = IngestResult(
                    source_id=prepared.source_id,
                    status="already_indexed",
                    note_id=str(note.metadata.id),
                )
            elif is_trivial_content(prepared.sanitized.text):
                results_by_source[prepared.source_id] = IngestResult(
                    source_id=prepared.source_id,
                    status="skipped_trivial",
                )
            else:
                candidates.append(prepared)

        if not candidates:
            return [results_by_source[record.source_id] for record in records]

        sources = [
            {
                "source_id": prepared.source_id,
                "title": prepared.title,
                "event_start": str(prepared.reference.event_start),
                "event_end": str(prepared.reference.event_end),
                "content": _bounded_extraction_text(
                    prepared.sanitized.text,
                    self._extraction_max_chars,
                ),
            }
            for prepared in candidates
        ]
        try:
            knowledge_by_source = self._extract_batch(sources, timeout_seconds)
            missing_sources = len(candidates) - len(knowledge_by_source)
            if missing_sources:
                _LOGGER.warning(
                    "Batch extraction partial sources=%d returned=%d",
                    len(candidates),
                    len(knowledge_by_source),
                )
        except (GatewayError, httpx.HTTPError) as error:
            if not allow_fallback:
                raise
            _LOGGER.warning(
                "Batch extraction fallback sources=%d error=%s detail=%s",
                len(candidates),
                error.__class__.__name__,
                _safe_error_detail(error),
            )
            knowledge_by_source = {}

        for prepared in candidates:
            knowledge = knowledge_by_source.get(prepared.source_id)
            status = "promoted"
            extraction_status = "extracted"
            if knowledge is None:
                if not allow_fallback:
                    results_by_source[prepared.source_id] = IngestResult(
                        source_id=prepared.source_id,
                        status="failed",
                        error="Batch extraction omitted source.",
                        llm_called=True,
                    )
                    continue
                knowledge = self._fallback_knowledge(
                    prepared.record,
                    prepared.title,
                    prepared.sanitized.text,
                )
                status = "promoted_fallback"
                extraction_status = "fallback"
            knowledge = self._sanitize_knowledge(
                knowledge,
                prepared.sanitized.text,
                prepared.reference,
            )
            if prepared.injection_detected:
                status = "promoted_quarantined"
            note = self._promote(
                prepared.space_id,
                knowledge,
                prepared.reference,
                quarantined=prepared.injection_detected,
                extraction_status=extraction_status,
            )
            results_by_source[prepared.source_id] = IngestResult(
                source_id=prepared.source_id,
                status=status,
                note_id=str(note.metadata.id),
                llm_called=True,
            )
        return [results_by_source[record.source_id] for record in records]

    def _extract_batch(
        self,
        sources: list[dict[str, str]],
        timeout_seconds: float | None,
    ) -> dict[str, ExtractedKnowledge]:
        """Call a production gateway with a bounded deadline when supported."""
        if isinstance(self._gateway, GatewayClient):
            return self._gateway.extract_batch(
                sources,
                timeout_seconds=timeout_seconds,
            )
        return self._gateway.extract_batch(sources)

    def content_hash(self, record: SourceRecord) -> str:
        """Return the deterministic sanitized hash for one source record."""
        return self._sanitizer.sanitize(record.content).source_hash

    def requires_extraction(
        self,
        record: SourceRecord,
        extract: bool = True,
        force_reextract: bool = False,
    ) -> bool:
        """Return whether a record would make a gateway extraction request."""
        if not extract:
            return False
        sanitized = self._sanitizer.sanitize(record.content)
        if is_trivial_content(sanitized.text):
            return False
        source_id = _safe_identifier_value(self._sanitizer, record.source_id)
        if force_reextract:
            return True
        note = self._note_index.by_source(source_id)
        if (
            note
            and any(
                source_ref.content_hash == sanitized.source_hash
                for source_ref in note.metadata.source_refs
            )
            and note.metadata.extraction_status != "fallback"
        ):
            return False
        if note is None and self._note_index.by_hash(sanitized.source_hash):
            return False
        return True

    def _prepare_record(self, record: SourceRecord) -> _PreparedRecord:
        """Sanitize and persist one source before any model request."""
        sanitized = self._sanitizer.sanitize(record.content)
        injection_detected = any(
            self._sanitizer.contains_prompt_injection(value)
            for value in (record.content, record.title, record.locator)
        )
        source_id = _safe_identifier_value(self._sanitizer, record.source_id)
        title = _safe_metadata_value(self._sanitizer, record.title)
        locator = _safe_identifier_value(self._sanitizer, record.locator)
        space_id = _safe_metadata_value(self._sanitizer, record.space_id)
        reference = SourceReference(
            id=source_id,
            locator=locator,
            content_hash=sanitized.source_hash,
            occurred_on=record.occurred_on,
            session_id=record.session_id,
            segment_id=record.segment_id,
            event_start=record.event_start,
            event_end=record.event_end,
        )
        self._write_sanitized_source(
            record,
            title,
            space_id,
            sanitized.text,
            reference,
        )
        return _PreparedRecord(
            record=record,
            sanitized=sanitized,
            injection_detected=injection_detected,
            source_id=source_id,
            title=title,
            locator=locator,
            space_id=space_id,
            reference=reference,
        )

    def _current_note(
        self,
        prepared: _PreparedRecord,
        force_reextract: bool,
    ) -> VaultNote | None:
        """Find an existing note that makes model extraction unnecessary."""
        if force_reextract:
            return None
        note = self._note_index.by_source(prepared.source_id)
        if (
            note
            and any(
                source_ref.content_hash == prepared.sanitized.source_hash
                for source_ref in note.metadata.source_refs
            )
            and note.metadata.extraction_status != "fallback"
        ):
            return note
        if note is None:
            hash_match = self._note_index.by_hash(prepared.sanitized.source_hash)
            if hash_match and hash_match.metadata.extraction_status != "fallback":
                return hash_match
        return None

    def promote(self, source_id: str) -> VaultNote:
        """Explicitly promote a reviewed candidate into the canonical Vault."""
        post = self._vault.read_review(source_id)
        if post is None:
            raise ValueError(f"No review candidate exists for '{source_id}'.")

        metadata = post.metadata
        knowledge = ExtractedKnowledge.model_validate(metadata["knowledge"])
        reference = SourceReference.model_validate(metadata["source_ref"])
        note = self._promote(str(metadata["space_id"]), knowledge, reference)
        self._vault.remove_review(source_id)
        return note

    def reject(self, source_id: str) -> None:
        """Discard a review candidate without touching its sanitized source."""
        if self._vault.read_review(source_id) is None:
            raise ValueError(f"No review candidate exists for '{source_id}'.")
        self._vault.remove_review(source_id)

    def ingest_file(
        self,
        path: Path,
        space_id: str,
        extract: bool = True,
    ) -> list[IngestResult]:
        """Ingest one text, Markdown, JSON, or JSON Lines source file."""
        records = list(_records_from_file(path, space_id))
        return [self.ingest(record, extract=extract) for record in records]

    def _extract(
        self,
        record: SourceRecord,
        title: str,
        sanitized_text: str,
        reference: SourceReference,
        extract: bool,
    ) -> ExtractedKnowledge:
        """Return gateway extraction or a deterministic fallback representation."""
        if extract:
            if self._gateway is None:
                raise RuntimeError("A gateway is required when extraction is enabled.")
            return self._gateway.extract(
                _extraction_payload(
                    title,
                    reference,
                    _bounded_extraction_text(
                        sanitized_text,
                        self._extraction_max_chars,
                    ),
                )
            )

        return self._fallback_knowledge(record, title, sanitized_text)

    def _fallback_knowledge(
        self,
        record: SourceRecord,
        title: str,
        sanitized_text: str,
    ) -> ExtractedKnowledge:
        """Create a deterministic note when gateway extraction is unavailable."""
        lines = sanitized_text.strip().splitlines()
        summary = lines[0] if lines else title
        return ExtractedKnowledge(
            title=title,
            note_type="task",
            summary=summary[:500],
            confidence=0.3,
            evidence_status="unknown",
            prompt_version=FALLBACK_PROMPT_VERSION,
            model_version=self._model_version,
        )

    def _write_sanitized_source(
        self,
        record: SourceRecord,
        title: str,
        space_id: str,
        content: str,
        reference: SourceReference,
    ) -> None:
        """Persist sanitized source material only."""
        self._sanitized_dir.mkdir(parents=True, exist_ok=True)
        path = self._sanitized_dir / f"{_safe_id(record.source_id)}.md"
        post = frontmatter.Post(
            content,
            source_id=reference.id,
            locator=reference.locator,
            content_hash=reference.content_hash,
            title=title,
            space_id=space_id,
            occurred_on=reference.occurred_on.isoformat()
            if reference.occurred_on
            else None,
        )
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def _backfill_occurred_on(
        self,
        note: VaultNote,
        reference: SourceReference,
    ) -> None:
        """Add source dates to an existing canonical note without re-extracting it."""
        if reference.occurred_on is None:
            return
        for source_ref in note.metadata.source_refs:
            if source_ref.id != reference.id or source_ref.occurred_on is not None:
                continue
            source_ref.occurred_on = reference.occurred_on
            self._vault.update_metadata(note)
            return

    def _promote(
        self,
        space_id: str,
        knowledge: ExtractedKnowledge,
        reference: SourceReference,
        quarantined: bool = False,
        extraction_status: ExtractionStatus = "extracted",
        source_content: str | None = None,
    ) -> VaultNote:
        """Write a canonical note from a sanitized extraction."""
        labels = knowledge.labels
        if self._label_registry is not None:
            labels = self._label_registry.canonicalize(labels)
        metadata = NoteMetadata(
            schema_version=2,
            type=knowledge.note_type,
            title=knowledge.title,
            space_id=space_id,
            ingested_at=datetime.now(UTC),
            source_refs=[reference],
            confidence=knowledge.confidence,
            links=knowledge.links,
            labels=labels,
            evidence_status=knowledge.evidence_status,
            recommendation_state=(
                "quarantined" if quarantined else _recommendation_state(knowledge)
            ),
            prompt_version=knowledge.prompt_version,
            model_version=knowledge.model_version,
            extraction_status=extraction_status,
            claims=knowledge.claims,
            actions=knowledge.actions,
            knowledge_level=knowledge.knowledge_level,
            pattern_key=knowledge.pattern_key,
            scope=knowledge.scope,
        )
        existing = self._note_index.by_source(reference.id)
        if existing:
            metadata.id = existing.metadata.id
            metadata.created_at = existing.metadata.created_at
            metadata.manual_labels = existing.metadata.manual_labels
            if existing.metadata.superseded_by:
                metadata.superseded_by = existing.metadata.superseded_by
        note = self._vault.upsert_managed(
            metadata,
            _render_managed_content(knowledge, source_content=source_content),
        )
        self._note_index.replace(note)
        return note

    def _sanitize_knowledge(
        self,
        knowledge: ExtractedKnowledge,
        source_text: str,
        reference: SourceReference,
    ) -> ExtractedKnowledge:
        """Sanitize model output and retain only verifiable claim evidence."""
        claims: list[Claim] = []
        for claim in knowledge.claims:
            claim.text = self._sanitizer.sanitize(claim.text).text[:1000]
            claim.claim_key = self._sanitizer.sanitize(claim.claim_key).text[:250]
            valid_evidence: list[EvidenceSpan] = []
            for evidence in claim.evidence:
                fragment = self._sanitizer.sanitize(evidence.fragment).text[:500]
                if not _fragment_in_source(fragment, source_text):
                    continue
                if evidence.source_id != reference.id:
                    continue
                if not _event_range_is_valid(evidence, reference, source_text):
                    continue
                evidence.fragment = fragment
                evidence.fragment_hash = hashlib.sha256(
                    fragment.encode("utf-8")
                ).hexdigest()
                evidence.session_id = reference.session_id
                evidence.segment_id = reference.segment_id
                if evidence.event_start is None:
                    evidence.event_start = reference.event_start
                if evidence.event_end is None:
                    evidence.event_end = reference.event_end
                evidence.precision = (
                    "exact"
                    if (
                        evidence.event_start is not None
                        and evidence.event_end is not None
                    )
                    else "source"
                )
                valid_evidence.append(evidence)
            claim.evidence = valid_evidence
            if not valid_evidence:
                claim.claim_type = "assistant_suggestion"
            claims.append(claim)
        knowledge.claims = claims
        knowledge.title = self._sanitizer.sanitize(knowledge.title).text.strip()[:300]
        knowledge.summary = self._sanitizer.sanitize(knowledge.summary).text[:2000]
        knowledge.facts = [
            self._sanitizer.sanitize(fact).text[:1000] for fact in knowledge.facts
        ]
        knowledge.links = [
            self._sanitizer.sanitize(link).text[:300] for link in knowledge.links
        ]
        knowledge.pattern_key = self._sanitizer.sanitize(
            knowledge.pattern_key
        ).text[:250]
        for field, value in knowledge.scope.model_dump().items():
            if field == "confidence":
                continue
            setattr(
                knowledge.scope,
                field,
                self._sanitizer.sanitize(str(value)).text[:200],
            )
        sanitized_actions = []
        for action in knowledge.actions:
            action.action_key = self._sanitizer.sanitize(action.action_key).text[:250]
            action.canonical_action_key = self._sanitizer.sanitize(
                action.canonical_action_key or action.action_key
            ).text[:250]
            action.subjects = [
                self._sanitizer.sanitize(item).text[:200] for item in action.subjects
            ]
            action.objects = [
                self._sanitizer.sanitize(item).text[:200] for item in action.objects
            ]
            action.tools = [
                self._sanitizer.sanitize(item).text[:200] for item in action.tools
            ]
            action.route = self._sanitizer.sanitize(action.route).text[:250]
            action.outcome = self._sanitizer.sanitize(action.outcome).text[:500]
            action.canonical_action_key = canonicalize_action_key(action)
            if action.action_key.strip():
                sanitized_actions.append(action)
        knowledge.actions = sanitized_actions
        if knowledge.prompt_version in {"", "v1"}:
            knowledge.prompt_version = EXTRACTION_PROMPT_VERSION
        knowledge.model_version = knowledge.model_version or self._model_version
        return knowledge


def _records_from_file(path: Path, default_space: str) -> list[SourceRecord]:
    """Convert supported source file formats into source records."""
    content = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [
            _record_from_mapping(json.loads(line), path, default_space)
            for line in content.splitlines()
            if line.strip()
        ]
    if path.suffix == ".json":
        payload = json.loads(content)
        if isinstance(payload, list):
            return [_record_from_mapping(item, path, default_space) for item in payload]
        return [_record_from_mapping(payload, path, default_space)]

    source_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return [
        SourceRecord(
            source_id=source_id,
            title=path.stem.replace("-", " ").replace("_", " ").title(),
            content=content,
            space_id=default_space,
            locator=str(path),
        )
    ]


def _record_from_mapping(
    payload: object,
    path: Path,
    default_space: str,
) -> SourceRecord:
    """Validate a JSON object accepted by the portable ingestion contract."""
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise ValueError(f"Invalid source record in '{path}'.")
    content = payload["content"]
    source_id = str(
        payload.get("id") or hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    )
    return SourceRecord(
        source_id=source_id,
        title=str(payload.get("title") or source_id),
        content=content,
        space_id=str(payload.get("space_id") or default_space),
        locator=str(payload.get("locator") or path),
    )


def _render_managed_content(
    knowledge: ExtractedKnowledge,
    source_content: str | None = None,
) -> str:
    """Render structured extraction into an Obsidian-readable managed block."""
    lines = ["## Summary", knowledge.summary.strip()]
    lines.extend(
        [
            "",
            "## Knowledge Scope",
            f"- level: {knowledge.knowledge_level}",
            f"- pattern_key: {knowledge.pattern_key or 'none'}",
        ]
    )
    for field, value in knowledge.scope.model_dump().items():
        if field != "confidence" and value:
            lines.append(f"- {field}: {value}")
    if knowledge.facts:
        lines.extend(["", "## Facts"])
        lines.extend(f"- {fact}" for fact in knowledge.facts)
    if knowledge.links:
        lines.extend(["", "## Related"])
        lines.extend(f"- [[{link}]]" for link in knowledge.links)
    if knowledge.actions:
        lines.extend(["", "## Actions"])
        for action in knowledge.actions:
            details = ", ".join(
                item
                for item in (
                    (
                        f"subjects: {', '.join(action.subjects)}"
                        if action.subjects
                        else ""
                    ),
                    (
                        f"objects: {', '.join(action.objects)}"
                        if action.objects
                        else ""
                    ),
                    (
                        f"tools: {', '.join(action.tools)}"
                        if action.tools
                        else ""
                    ),
                    f"route: {action.route}" if action.route else "",
                )
                if item
            )
            lines.append(
                f"- {action.action_key} ({details})"
                if details
                else f"- {action.action_key}"
            )
    if knowledge.claims:
        lines.extend(["", "## Claims"])
        for claim in knowledge.claims:
            evidence = (
                ", ".join(evidence.source_id for evidence in claim.evidence)
                or "unverified"
            )
            lines.append(f"- [{claim.claim_type}] {claim.text} (evidence: {evidence})")
    if source_content:
        lines.extend(["", "## Source", source_content.strip()])
    return "\n".join(lines).strip()


def _safe_id(value: str) -> str:
    """Return a filesystem-safe identifier without accepting traversal."""
    return (
        "".join(
            character if character.isalnum() or character in ("-", "_") else "-"
            for character in value
        ).strip("-")
        or "source"
    )


def source_date_from_locator(locator: str) -> date | None:
    """Infer a Codex session date from its local rollout locator."""
    match = re.match(r"^codex-session://(\d{4})/(\d{2})/(\d{2})/", locator)
    if match is None:
        return None
    try:
        return date.fromisoformat("-".join(match.groups()))
    except ValueError:
        return None


def _bounded_extraction_text(content: str, maximum_chars: int) -> str:
    """Bound gateway input while retaining both session opening and conclusion."""
    if len(content) <= maximum_chars:
        return content
    half = maximum_chars // 2
    return (
        f"{content[:half]}\n\n[Session truncated for extraction]\n\n{content[-half:]}"
    )


def is_trivial_content(content: str) -> bool:
    """Return whether content is too trivial to warrant model extraction.

    The rule is deliberately conservative: short content without structural or
    work-related signals is skipped, while longer prose is retained for review
    by the extraction model. It does not depend on a model or one language.
    """
    normalized = _MESSAGE_PREFIX_RE.sub("", content)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    if not normalized:
        return True
    if normalized in _TRIVIAL_ACKNOWLEDGEMENTS:
        return True
    if len(normalized) <= 240 and not _TECHNICAL_SIGNAL_RE.search(normalized):
        return True
    return False


def _safe_error_detail(error: Exception) -> str:
    """Return bounded non-sensitive extraction error context for logs."""
    detail = str(error).replace("\n", " ").strip()
    return detail[:240] or "none"


def _safe_metadata_value(sanitizer: Sanitizer, value: str) -> str:
    """Return one sanitized, single-line metadata value."""
    sanitized = sanitizer.sanitize(value).text.replace("\n", " ").strip()
    return sanitized[:1000] or "unknown"


def _safe_identifier_value(sanitizer: Sanitizer, value: str) -> str:
    """Sanitize identifiers without destroying stable opaque path components."""
    sanitized = sanitizer.sanitize_metadata(value).text.replace("\n", " ").strip()
    return sanitized[:1000] or "unknown"


def _extraction_payload(
    title: str,
    reference: SourceReference,
    content: str,
) -> str:
    """Add non-sensitive provenance context to an extraction request."""
    return "\n".join(
        [
            f"SOURCE_ID: {reference.id}",
            f"TITLE: {title}",
            f"EVENT_START: {reference.event_start}",
            f"EVENT_END: {reference.event_end}",
            "CONTENT:",
            content,
        ]
    )


def _fragment_in_source(fragment: str, source_text: str) -> bool:
    """Return whether a normalized evidence fragment occurs in the source."""
    normalized_fragment = " ".join(fragment.split()).lower()
    normalized_source = " ".join(source_text.split()).lower()
    return bool(normalized_fragment) and normalized_fragment in normalized_source


def _event_range_is_valid(
    evidence: EvidenceSpan,
    reference: SourceReference,
    source_text: str,
) -> bool:
    """Reject model-provided event ranges outside the source segment."""
    if evidence.event_start is None or evidence.event_end is None:
        return True
    if evidence.event_start > evidence.event_end:
        return False
    if (
        reference.event_start is not None
        and evidence.event_start < reference.event_start
    ):
        return False
    if reference.event_end is not None and evidence.event_end > reference.event_end:
        return False
    event_indices = [
        int(match.group(1)) for match in re.finditer(r"\bevent=(\d+)\b", source_text)
    ]
    if not event_indices:
        return True
    return evidence.event_start >= min(event_indices) and evidence.event_end <= max(
        event_indices
    )


def _recommendation_state(knowledge: ExtractedKnowledge) -> str:
    """Classify extracted knowledge for ranking and workflow eligibility."""
    if knowledge.confidence < 0.7:
        return "penalized"
    if knowledge.evidence_status in {"unknown", "proposal", "investigation"}:
        return "penalized"
    if any(not claim.evidence for claim in knowledge.claims):
        return "penalized"
    return "active"
