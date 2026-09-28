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


def test_mcp_tools_are_async(tmp_path: Path) -> None:
    """All registered MCP tools are async to avoid blocking the event loop."""
    import inspect

    server = create_server(make_settings(tmp_path / "brain"))
    for tool_name, tool in server._tool_manager._tools.items():
        assert inspect.iscoroutinefunction(tool.fn), f"Tool {tool_name} must be async"


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
        result = server._tool_manager.get_tool(name).fn(**arguments)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

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
    assert (
        call("brain_list_by_date", start_on="2026-08-01", end_on="2026-08-02")["status"]
        == "not_found"
    )
    assert (
        call("brain_list_by_date", start_on="2026-08-01", end_on="2026-08-02")["meta"][
            "abstention_reason"
        ]
        == "no_notes_in_date_range"
    )
    timeline = call("brain_list_by_date", start_on="2026-08-01", end_on="2026-08-02")
    assert timeline["meta"]["date_basis"] == "source_reference.occurred_on"
    assert timeline["meta"]["coverage_warning"] == ("some_notes_have_no_source_date")
    assert timeline["meta"]["total_count"] == 0
    assert timeline["meta"]["has_more"] is False
    assert call("brain_list_by_label", labels=["terraform"])["status"] == "not_found"
    assert call("brain_recommend_workflow", task="deploy")["status"] == "abstained"
    assert call("brain_get_workflow", workflow_id="missing")["status"] == "not_found"
    assert (
        call("brain_record_feedback", workflow_id="missing", outcome="failed")["status"]
        == "ok"
    )
    assert (
        call(
            "brain_record_search_feedback",
            query="delete dataset",
            note_ids=["missing"],
            relevance="irrelevant",
        )["status"]
        == "stored"
    )
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


def test_mcp_server_registers_dashboard_routes(tmp_path: Path) -> None:
    """The MCP server registers /, /dashboard, and /api/dashboard custom routes."""
    server = create_server(make_settings(tmp_path / "brain"))
    routes = [route.path for route in server._custom_starlette_routes]
    assert "/" in routes
    assert "/dashboard" in routes
    assert "/api/dashboard" in routes


def test_access_matrix_resolution() -> None:
    """Access matrix resolves allowed spaces deterministically by user identity."""
    from exocortex.mcp_server import resolve_allowed_spaces, resolve_remember_space

    # user_id == 'fsirio'
    assert resolve_allowed_spaces("fsirio") == ["personal-fsirio", "shared", "work"]
    assert resolve_allowed_spaces("FSIRIO") == ["personal-fsirio", "shared", "work"]
    assert resolve_allowed_spaces("fsirio", space_id="personal") == ["personal-fsirio"]
    assert resolve_allowed_spaces("fsirio", space_id="shared") == ["shared"]
    assert resolve_allowed_spaces("fsirio", space_id="work") == ["work"]
    assert resolve_allowed_spaces("fsirio", space_id="personal-mercedes") == []

    # user_id == 'mercedes'
    assert resolve_allowed_spaces("mercedes") == ["personal-mercedes", "shared"]
    assert resolve_allowed_spaces("Mercedes") == ["personal-mercedes", "shared"]
    assert resolve_allowed_spaces("mercedes", space_id="personal") == [
        "personal-mercedes"
    ]
    assert resolve_allowed_spaces("mercedes", space_id="shared") == ["shared"]
    # Mercedes is forbidden from 'work' or 'personal-fsirio'
    assert resolve_allowed_spaces("mercedes", space_id="work") == []
    assert resolve_allowed_spaces("mercedes", space_id="personal-fsirio") == []

    # user_id == None
    assert resolve_allowed_spaces(None) == ["work"]
    assert resolve_allowed_spaces(None, space_id="personal") == ["work"]
    assert resolve_allowed_spaces(None, space_id="work") == ["work"]
    assert resolve_allowed_spaces(None, space_id="personal-fsirio") == []

    # Remember space resolution
    assert resolve_remember_space("mercedes", "personal") == "personal-mercedes"
    assert resolve_remember_space("mercedes", "shared") == "shared"
    assert resolve_remember_space("mercedes", None) == "personal-mercedes"
    assert resolve_remember_space("fsirio", "personal") == "personal-fsirio"
    assert resolve_remember_space("fsirio", "work") == "work"
    assert resolve_remember_space("fsirio", None) == "work"
    assert resolve_remember_space(None, "personal") == "work"
    assert resolve_remember_space(None, None) == "work"


def test_mcp_tools_propagate_user_id_and_filter_privacies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """MCP tools receive user_id and isolate user memories."""

    class CapturingService:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.search_kwargs = {}
            self.remember_kwargs = {}
            self.notes = {
                "fsirio-note": VaultNote(
                    metadata=NoteMetadata(
                        type="task",
                        title="Fsirio Secret",
                        space_id="personal-fsirio",
                        owner="fsirio",
                    ),
                    content="Confidential Fsirio Task",
                    path="Vault/personal-fsirio/Tasks/fsirio-note.md",
                ),
                "mercedes-note": VaultNote(
                    metadata=NoteMetadata(
                        type="task",
                        title="Mercedes Health",
                        space_id="personal-mercedes",
                        owner="mercedes",
                    ),
                    content="Confidential Mercedes Health",
                    path="Vault/personal-mercedes/Tasks/mercedes-note.md",
                ),
                "shared-note": VaultNote(
                    metadata=NoteMetadata(
                        type="task",
                        title="Honda Fit Service",
                        space_id="shared",
                    ),
                    content="Car Maintenance",
                    path="Vault/shared/Tasks/shared-note.md",
                ),
            }

        def search_response(self, *args, **kwargs) -> ResponseEnvelope:
            self.search_kwargs = kwargs
            return ResponseEnvelope(status="ok", method="search", data=[])

        def get_note(self, note_id: str):
            return self.notes.get(note_id)

        def remember_response(self, **kwargs) -> ResponseEnvelope:
            self.remember_kwargs = kwargs
            return ResponseEnvelope(
                status="stored",
                method="remember",
                data={"note_id": "new-note", "note_path": "new-path"},
            )

        def notes_by_date(self, *args, **kwargs):
            return []

        def date_coverage(self, *args, **kwargs):
            return {
                "notes_scanned": 0,
                "notes_with_source_refs": 0,
                "source_refs_with_dates": 0,
                "source_refs_in_range": 0,
                "notes_without_source_dates": 0,
                "notes_created_in_range": 0,
                "notes_updated_in_range": 0,
                "notes_ingested_in_range": 0,
                "notes_in_range": 0,
            }

        def list_by_label(self, *args, **kwargs):
            return []

        def recommend_workflow_response(self, *args, **kwargs) -> ResponseEnvelope:
            return ResponseEnvelope(status="abstained", method="workflow", data=[])

    capturing_service = CapturingService(make_settings(tmp_path / "brain"))
    monkeypatch.setattr(mcp_server, "BrainService", lambda s: capturing_service)

    server = create_server(make_settings(tmp_path / "brain"))

    def call(name: str, **arguments):
        result = server._tool_manager.get_tool(name).fn(**arguments)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

    # 1. Search as mercedes -> allowed_spaces must be ['personal-mercedes', 'shared']
    call("brain_search", query="health", user_id="mercedes")
    assert capturing_service.search_kwargs["allowed_spaces"] == [
        "personal-mercedes",
        "shared",
    ]

    # 2. Search as fsirio -> allowed_spaces must include work and shared
    call("brain_search", query="work project", user_id="fsirio")
    assert capturing_service.search_kwargs["allowed_spaces"] == [
        "personal-fsirio",
        "shared",
        "work",
    ]

    # 3. Search without user_id -> fallback to ['work']
    call("brain_search", query="generic query")
    assert capturing_service.search_kwargs["allowed_spaces"] == ["work"]

    # 4. Remember with space_id='personal' as mercedes -> 'personal-mercedes'
    call(
        "brain_remember",
        content="Swimming session",
        title="Swim",
        space_id="personal",
        user_id="mercedes",
    )
    assert capturing_service.remember_kwargs["space_id"] == "personal-mercedes"
    assert capturing_service.remember_kwargs["owner"] == "mercedes"

    # 5. Remember with space_id='personal' as fsirio -> resolves to 'personal-fsirio'
    call(
        "brain_remember",
        content="EDC Knife",
        title="Knife",
        space_id="personal",
        user_id="fsirio",
    )
    assert capturing_service.remember_kwargs["space_id"] == "personal-fsirio"
    assert capturing_service.remember_kwargs["owner"] == "fsirio"

    # 6. brain_get isolation: Mercedes CANNOT view Fsirio note
    mercedes_access_fsirio = call(
        "brain_get", note_id="fsirio-note", user_id="mercedes"
    )
    assert mercedes_access_fsirio["status"] == "not_found"

    # 7. brain_get isolation: Fsirio CANNOT view Mercedes note
    fsirio_access_mercedes = call(
        "brain_get", note_id="mercedes-note", user_id="fsirio"
    )
    assert fsirio_access_mercedes["status"] == "not_found"

    # 8. brain_get isolation: Mercedes CAN view shared note and her own note
    mercedes_own = call("brain_get", note_id="mercedes-note", user_id="mercedes")
    assert mercedes_own["status"] == "ok"
    shared_by_mercedes = call("brain_get", note_id="shared-note", user_id="mercedes")
    assert shared_by_mercedes["status"] == "ok"
