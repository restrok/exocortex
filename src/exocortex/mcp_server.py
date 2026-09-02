"""Streamable HTTP MCP server for Codex Brain."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from exocortex.config import Settings
from exocortex.models import Claim, EvidenceSpan, ResponseEnvelope, VaultNote
from exocortex.service import BrainService


def _display_note_with_claims(note: VaultNote) -> tuple[VaultNote, str]:
    """Expose source-backed derived claims without overstating extraction."""
    if note.metadata.claims:
        return note, "present"
    if not note.metadata.source_refs:
        return note, "not_extracted"

    statements = [
        line.strip()[2:]
        for line in note.content.splitlines()
        if line.strip().startswith("- ") and line.strip()[2:].strip()
    ][:20]
    if not statements:
        summary = " ".join(
            line.strip()
            for line in note.content.splitlines()
            if line.strip() and not line.strip().startswith("<!--")
        )
        if summary:
            statements = [summary[:500]]
    if not statements:
        return note, "not_extracted"

    source = note.metadata.source_refs[0]
    claims = [
        Claim(
            id=f"derived-{hashlib.sha256(statement.encode()).hexdigest()[:16]}",
            text=statement,
            claim_key=f"source_statement.{index}",
            claim_type="brain_derived",
            confidence=min(note.metadata.confidence, 0.6),
            evidence=[
                EvidenceSpan(
                    source_id=source.id,
                    fragment=statement,
                    fragment_hash=hashlib.sha256(statement.encode()).hexdigest(),
                    precision="source",
                )
            ],
        )
        for index, statement in enumerate(statements, 1)
    ]
    metadata = note.metadata.model_copy(update={"claims": claims})
    return note.model_copy(update={"metadata": metadata}), "derived_from_source"


def create_server(settings: Settings | None = None) -> FastMCP:
    """Build the local work MCP server exposing grounded brain operations."""
    settings = settings or Settings()
    service = BrainService(settings)

    mcp = FastMCP(
        name="Codex Work Brain",
        instructions=(
            "Use brain_recommend_workflow before executing a repeated task, "
            "then use brain_search for historical context. Deprioritized "
            "workflows are weakly relevant to the configured operational "
            "context and should not be selected automatically. Other "
            "workflows below 0.80 require user confirmation; workflows at "
            "or above 0.80 may be applied automatically. After confirmation "
            "or execution, "
            "report the outcome with brain_record_feedback. Cite returned "
            "note paths and evidence references. "
            "Use brain_remember only for durable knowledge and tell the user "
            "that it was stored in the local work Vault."
            " brain_search defaults to conservative answer mode: use direct "
            "evidence for claims and treat context_only results as context. "
            "Use answer_mode=exploration when broader related candidates are "
            "needed. Prefer pattern-level knowledge for generic questions and "
            "adapter/example knowledge only when the provider, runtime, region, "
            "or organization is explicit."
        ),
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_search(
        query: str,
        space_id: str | None = None,
        project_id: str | None = None,
        repository_id: str | None = None,
        limit: int = 5,
        answer_mode: str = "conservative",
        include_candidates: bool = False,
    ) -> dict[str, Any]:
        """Search prior knowledge and return source-grounded results."""
        return service.search_response(
            query,
            space_id=space_id,
            project_id=project_id,
            repository_id=repository_id,
            limit=limit,
            answer_mode=answer_mode,
            include_candidates=include_candidates,
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_get(note_id: str) -> dict[str, Any]:
        """Return a canonical note by its stable identifier."""
        note = service.get_note(note_id)
        display_note, claim_status = (
            _display_note_with_claims(note) if note else (None, "not_extracted")
        )
        incomplete = note is not None and not note.content.strip()
        response = ResponseEnvelope(
            status="incomplete" if incomplete else "ok" if note else "not_found",
            method="vault-get",
            data=(
                display_note.model_dump(mode="json")
                if display_note and not incomplete
                else None
            ),
            meta=(
                {
                    "claim_status": claim_status,
                    "claim_count": len(display_note.metadata.claims)
                    if display_note and not incomplete
                    else 0,
                    "source_ref_count": len(note.metadata.source_refs) if note else 0,
                    "integrity_status": "empty_content" if incomplete else "complete",
                    "usable_as_evidence": not incomplete,
                    "note_id": note_id if incomplete else None,
                    "path": note.path if incomplete else None,
                }
                if note
                else {}
            ),
        )
        return response.model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_list_by_date(
        start_on: str,
        end_on: str,
        space_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List notes by source-conversation date using YYYY-MM-DD bounds."""
        try:
            start_date = date.fromisoformat(start_on)
            end_date = date.fromisoformat(end_on)
        except ValueError as error:
            raise ValueError("Dates must use YYYY-MM-DD.") from error
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        if offset < 0:
            raise ValueError("offset must be non-negative.")
        coverage = service.date_coverage(start_date, end_date, space_id=space_id)
        data = [
            result.model_dump(mode="json")
            for result in service.notes_by_date(
                start_date,
                end_date,
                space_id=space_id,
                limit=limit,
                offset=offset,
            )
        ]
        total_count = int(coverage["notes_in_range"])
        has_more = offset + len(data) < total_count
        return ResponseEnvelope(
            status="ok" if data else "not_found",
            method="timeline",
            data=data,
            meta={
                "limit": limit,
                "offset": offset,
                "total_count": total_count,
                "has_more": has_more,
                "next_offset": offset + len(data) if has_more else None,
                "start_on": start_on,
                "end_on": end_on,
                "result_count": len(data),
                "date_basis": "source_reference.occurred_on",
                "coverage": coverage,
                "coverage_warning": (
                    "notes_exist_by_ingestion_date_but_not_by_source_date"
                    if not data and coverage["notes_created_in_range"]
                    else "some_notes_have_no_source_date"
                    if coverage["notes_without_source_dates"]
                    else None
                ),
                "abstention_reason": (
                    None
                    if data
                    else "offset_out_of_range"
                    if total_count and offset >= total_count
                    else "no_notes_in_date_range"
                ),
            },
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_list_by_label(
        labels: list[str],
        space_id: str | None = None,
        match_all: bool = False,
        limit: int = 25,
    ) -> dict[str, Any]:
        """List notes connected to one or more canonical labels."""
        data = [
            result.model_dump(mode="json")
            for result in service.list_by_label(
                labels,
                space_id=space_id,
                match_all=match_all,
                limit=limit,
            )
        ]
        return ResponseEnvelope(
            status="ok" if data else "not_found",
            method="labels",
            data=data,
            meta={"limit": limit},
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_recommend_workflow(
        task: str,
        space_id: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Recommend active workflows relevant to a task or problem."""
        return service.recommend_workflow_response(
            task,
            space_id=space_id,
            limit=limit,
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_get_workflow(workflow_id: str) -> dict[str, Any]:
        """Return one active workflow with its evidence references."""
        note = service.get_workflow(workflow_id)
        return ResponseEnvelope(
            status="ok" if note else "not_found",
            method="workflow-get",
            data=note.model_dump(mode="json") if note else None,
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def brain_record_feedback(
        workflow_id: str,
        outcome: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Update a workflow confidence score from a reported outcome."""
        return service.record_workflow_feedback(
            workflow_id,
            outcome,
            notes,
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def brain_record_search_feedback(
        query: str,
        note_ids: list[str],
        relevance: str,
        reason: str | None = None,
        space_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist explicit relevance feedback for search results."""
        return service.record_search_feedback(
            query=query,
            note_ids=note_ids,
            relevance=relevance,
            reason=reason,
            space_id=space_id,
            tags=tags,
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_health() -> dict[str, Any]:
        """Return non-sensitive health for Vault, gateway, and Neo4j."""
        report = service.doctor()
        return ResponseEnvelope(
            status=(
                "ok"
                if report.vault == "ok"
                and report.gateway == "ok"
                and report.neo4j == "ok"
                else "degraded"
            ),
            method="health",
            data={
                "vault": report.vault,
                "gateway": report.gateway,
                "neo4j": report.neo4j,
                "detail": report.detail,
            },
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def brain_learning_status() -> dict[str, Any]:
        """Return non-sensitive learning progress and active workflow count."""
        return ResponseEnvelope(
            status="ok",
            method="learning-status",
            data=service.learning_status(),
        ).model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def brain_ingest_session(
        transcript_jsonl: str,
        conversation_id: str,
        space_id: str | None = None,
    ) -> dict[str, Any]:
        """Ingest transcript JSONL over MCP without filesystem mounts."""
        response = service.ingest_antigravity_transcript(
            transcript_jsonl=transcript_jsonl,
            conversation_id=conversation_id,
            space_id=space_id or settings.default_space,
        )
        return response.model_dump(mode="json")

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        )
    )
    def brain_remember(
        content: str,
        title: str,
        space_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a sanitized durable memory in the canonical work Vault."""
        response = service.remember_response(
            content=content,
            title=title,
            space_id=space_id or settings.default_space,
        )
        return response.model_dump(mode="json")

    return mcp


def main() -> None:
    """Run the streamable HTTP MCP server."""
    server = create_server()
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
