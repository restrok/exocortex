"""Tests for the Codex Brain MCP tool surface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from exocortex import mcp_server
from exocortex.mcp_server import create_server
from exocortex.models import (
    NoteMetadata,
    ResponseEnvelope,
    SourceReference,
    VaultNote,
)
from tests.conftest import make_settings


def test_mcp_server_exposes_grounded_tools(tmp_path: Path) -> None:
    """The MCP server exposes grounded read and automatic-write operations."""
    server = create_server(make_settings(tmp_path / "brain"))

    tools = asyncio.run(server.list_tools())

    assert [tool.name for tool in tools] == [
        "brain_search",
        "brain_get",
        "brain_list_by_date",
        "brain_list_by_label",
        "brain_recommend_workflow",
        "brain_get_workflow",
        "brain_record_feedback",
        "brain_record_search_feedback",
        "brain_health",
        "brain_learning_status",
        "brain_ingest_session",
        "brain_remember",
    ]


def test_mcp_tools_return_v2_envelopes_and_validate_dates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Each registered tool delegates to the service with safe response metadata."""

    class FakeService:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.note = VaultNote(
                metadata=NoteMetadata(
                    type="task",
                    title="Memory",
                    space_id="work",
                    source_refs=[
                        SourceReference(
                            id="source-1",
                            locator="mcp://source",
                            content_hash="hash",
                        )
                    ],
                ),
                content="Memory",
                path="Vault/work/Tasks/memory.md",
            )
            self.empty_note = VaultNote(
                metadata=NoteMetadata(
                    type="task",
                    title="Incomplete",
                    space_id="work",
                ),
                content="",
                path="Vault/work/Tasks/incomplete.md",
            )

        def search_response(self, *args, **kwargs) -> ResponseEnvelope:
            return ResponseEnvelope(status="ok", method="search", data=[])

        def get_note(self, note_id: str):
            return {
                "present": self.note,
                "empty": self.empty_note,
            }.get(note_id)

        def notes_by_date(self, *args, **kwargs):
            return []

        def date_coverage(self, *args, **kwargs):
            return {
                "notes_scanned": 1,
                "notes_with_source_refs": 1,
                "source_refs_with_dates": 0,
                "source_refs_in_range": 0,
                "notes_without_source_dates": 1,
                "notes_created_in_range": 0,
                "notes_updated_in_range": 0,
                "notes_ingested_in_range": 0,
                "notes_in_range": 0,
            }

        def list_by_label(self, *args, **kwargs):
            return []

        def recommend_workflow_response(self, *args, **kwargs) -> ResponseEnvelope:
            return ResponseEnvelope(status="abstained", method="workflow", data=[])

        def get_workflow(self, workflow_id: str):
            return None

        def record_workflow_feedback(self, *args, **kwargs) -> ResponseEnvelope:
            return ResponseEnvelope(status="ok", method="workflow-feedback", data={})

        def record_search_feedback(self, *args, **kwargs) -> ResponseEnvelope:
            return ResponseEnvelope(status="stored", method="search-feedback", data={})

        def doctor(self):
            return SimpleNamespace(
                vault="ok",
                gateway="unavailable",
                neo4j="ok",
                detail={"gateway": "ConnectError"},
            )

        def learning_status(self):
            return {"processed_notes": 0, "pending_notes": 0, "active_workflows": 0}

        def remember_response(self, **kwargs) -> ResponseEnvelope:
            return ResponseEnvelope(
                status="stored",
                method="remember",
                data={
                    "note_id": str(self.note.metadata.id),
                    "note_path": self.note.path,
                },
            )

    monkeypatch.setattr(mcp_server, "BrainService", FakeService)
    server = create_server(make_settings(tmp_path / "brain"))
    remember_schema = server._tool_manager.get_tool("brain_remember").output_schema
    assert remember_schema["additionalProperties"] is True

    def call(name: str, **arguments):
        return server._tool_manager.get_tool(name).fn(**arguments)

    assert call("brain_search", query="terraform")["status"] == "ok"
    assert call("brain_get", note_id="missing")["status"] == "not_found"
    present = call("brain_get", note_id="present")
    assert present["meta"]["claim_status"] == "derived_from_source"
    assert present["meta"]["claim_count"] == 1
    assert present["data"]["metadata"]["claims"][0]["evidence"]
    incomplete = call("brain_get", note_id="empty")
    assert incomplete["status"] == "incomplete"
    assert incomplete["data"] is None
    assert incomplete["meta"]["usable_as_evidence"] is False
    assert call("brain_list_by_date", start_on="2026-08-01", end_on="2026-08-02")[
        "status"
    ] == "not_found"
    assert call("brain_list_by_date", start_on="2026-08-01", end_on="2026-08-02")[
        "meta"
    ]["abstention_reason"] == "no_notes_in_date_range"
    timeline = call(
        "brain_list_by_date", start_on="2026-08-01", end_on="2026-08-02"
    )
    assert timeline["meta"]["date_basis"] == "source_reference.occurred_on"
    assert timeline["meta"]["coverage_warning"] == (
        "some_notes_have_no_source_date"
    )
    assert timeline["meta"]["total_count"] == 0
    assert timeline["meta"]["has_more"] is False
    assert call("brain_list_by_label", labels=["terraform"])["status"] == "not_found"
    assert call("brain_recommend_workflow", task="deploy")["status"] == "abstained"
    assert call("brain_get_workflow", workflow_id="missing")["status"] == "not_found"
    assert call(
        "brain_record_feedback", workflow_id="missing", outcome="failed"
    )["status"] == "ok"
    assert call(
        "brain_record_search_feedback",
        query="delete dataset",
        note_ids=["missing"],
        relevance="irrelevant",
    )["status"] == "stored"
    assert call("brain_health")["status"] == "degraded"
    assert call("brain_learning_status")["status"] == "ok"
    assert call("brain_remember", content="memory", title="Memory")["status"] == (
        "stored"
    )

    try:
        call("brain_list_by_date", start_on="invalid", end_on="2026-08-02")
    except ValueError as error:
        assert str(error) == "Dates must use YYYY-MM-DD."
    else:
        raise AssertionError("invalid dates should be rejected")
