"""Tests for service orchestration branches around external dependencies."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from exocortex.ingest import SourceRecord
from exocortex.models import (
    Claim,
    LabelAliasProposal,
    NoteMetadata,
    ReflectionKnowledge,
    SearchResult,
)
from exocortex.service import BrainService, _note_fingerprint
from tests.conftest import make_settings


def _result(note_id: str, note_type: str = "task") -> SearchResult:
    """Build a minimal graph result for service orchestration tests."""
    return SearchResult(
        note_id=note_id,
        title="Terraform validation",
        note_type=note_type,
        space_id="work",
        path="Vault/work/Tasks/terraform-validation.md",
        score=1.0,
        excerpt="Terraform plan",
        confidence=0.95,
    )


def test_reflection_fingerprint_ignores_administrative_timestamp(
    tmp_path: Path,
) -> None:
    """Metadata maintenance must not make an unchanged experience pending."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Stable note", space_id="work"),
        "## Summary\nKeep this content.",
    )
    original = _note_fingerprint(note)

    note.metadata.updated_at = note.metadata.updated_at.replace(year=2030)

    assert _note_fingerprint(note) == original

    note.content = "## Summary\nChanged content."
    assert _note_fingerprint(note) != original


def test_service_search_lists_and_workflow_gates(tmp_path: Path, monkeypatch) -> None:
    """Search responses expose degradation while workflow reads stay gated."""
    service = BrainService(make_settings(tmp_path / "brain"))
    task = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform validation",
            space_id="work",
            labels=["technology:terraform"],
        ),
        "## Summary\nTerraform plan",
    )
    workflow = service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Terraform validation",
            space_id="work",
            confidence=0.95,
            workflow_steps=[{"text": "plan", "evidence_claim_ids": ["claim"]}],
        ),
        "## Summary\nTerraform plan",
    )
    graph_result = _result(str(task.metadata.id))
    workflow_result = _result(str(workflow.metadata.id), "workflow")
    workflow_result.claims = [
        Claim(
            id="claim",
            text="Terraform plan",
            claim_key="terraform.plan",
        )
    ]

    class Store:
        def search_fulltext(self, query, space_id, limit):
            return [graph_result]

        def search_vector(self, embedding, space_id, limit):
            return [graph_result]

        def close(self) -> None:
            return None

    monkeypatch.setattr(service, "_graph_store", lambda: Store())
    monkeypatch.setattr(service.gateway, "embed", lambda query: [0.1, 0.2])

    results = service.search("Terraform validation", space_id="work", limit=2)
    assert results[0].note_id == str(task.metadata.id)
    assert service.list_by_label(["technology:terraform"])[0].note_id == str(
        task.metadata.id
    )
    assert service.get_workflow(str(workflow.metadata.id)) is not None
    assert service.get_workflow(str(task.metadata.id)) is None

    monkeypatch.setattr(
        service,
        "_hybrid_search",
        lambda task_name, space_id, limit: ([workflow_result], False),
    )
    assert service.recommend_workflows("Terraform")
    assert service.recommend_workflow_response("Terraform").status == "ok"


def test_operational_context_ranks_specific_workflows_without_changing_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Context downranks generic procedures while retaining eligible workflows."""
    settings = make_settings(tmp_path / "brain")
    settings.operational_context_enabled = True
    settings.operational_role = "platform engineering"
    settings.operational_domains = "terraform,cloud-run"
    settings.operational_common_tasks = "deployment validation"
    settings.operational_preferred_tools = "terraform,gcloud"
    service = BrainService(settings)
    claim = Claim(id="claim", text="A command succeeded", claim_key="command.ok")

    specific = service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Validate a Terraform Cloud Run deployment",
            space_id="work",
            confidence=0.95,
            labels=["technology:terraform", "technology:cloud-run"],
            claims=[claim],
            workflow_steps=[
                {"text": "Run terraform plan", "evidence_claim_ids": ["claim"]}
            ],
        ),
        "## Summary\nValidate Terraform on Cloud Run.\n\n"
        "## Triggers\n- Before deployment\n\n"
        "## Validation\n- Confirm the plan and rollout status\n\n"
        "## Evidence\n- claim",
    )
    generic = service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Move work to a feature branch and publish it",
            space_id="work",
            confidence=0.95,
            claims=[claim],
            workflow_steps=[
                {
                    "text": "Create and publish the branch",
                    "evidence_claim_ids": ["claim"],
                }
            ],
        ),
        "## Summary\nMove work to a feature branch and publish it.\n\n"
        "## Evidence\n- claim",
    )
    candidates = [
        _result(str(generic.metadata.id), "workflow"),
        _result(str(specific.metadata.id), "workflow"),
    ]
    candidates[0].claims = [claim]
    candidates[1].claims = [claim]
    monkeypatch.setattr(
        service,
        "_hybrid_search",
        lambda task, space_id, limit: (candidates, False),
    )

    results = service.recommend_workflows("deployment", limit=5)

    assert [result.note_id for result in results] == [
        str(specific.metadata.id),
        str(generic.metadata.id),
    ]
    assert results[0].relevance_score > results[1].relevance_score
    assert results[1].recommendation_level == "deprioritize"
    assert results[1].evidence_score == 0.95


def test_service_degraded_search_audit_export_and_doctor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Local fallback and diagnostics remain safe when both services are absent."""
    service = BrainService(make_settings(tmp_path / "brain"))
    service.vault.upsert_managed(
        NoteMetadata(type="task", title="Local plan", space_id="work"),
        "## Summary\nRun the local plan.",
    )

    class BrokenStore:
        def search_fulltext(self, query, space_id, limit):
            raise RuntimeError("neo4j down")

        def search_vector(self, embedding, space_id, limit):
            raise RuntimeError("neo4j down")

        def verify_connectivity(self) -> None:
            raise RuntimeError("neo4j down")

        def close(self) -> None:
            return None

    monkeypatch.setattr(service, "_graph_store", lambda: BrokenStore())

    def gateway_down(*args: object, **kwargs: object) -> None:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(service.gateway, "embed", gateway_down)
    monkeypatch.setattr(service.gateway, "health", gateway_down)

    response = service.search_response("Local plan")
    assert response.status == "degraded"
    health = service.doctor()
    assert health.gateway == "unavailable"
    assert health.neo4j == "unavailable"
    assert service.learning_status()["pending_notes"] == 1
    assert service.audit() == []
    export_path = service.export(tmp_path / "exports")
    assert export_path.exists()


def test_service_sync_is_incremental_and_rebuilds_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Projection state prevents duplicate indexing and supports full rebuilds."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Sync me", space_id="work"),
        "## Summary\nSync me.",
    )
    indexed: list[tuple[str, bool]] = []

    class SyncStore:
        def ensure_schema(self, **kwargs: object) -> None:
            assert kwargs["embedding_dimensions"] is None

        def upsert_notes(self, rows) -> None:
            indexed.extend(
                (str(current.metadata.id), embedding is not None)
                for current, embedding in rows
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(service, "_graph_store", lambda: SyncStore())

    assert service.sync(embed=False) == 1
    assert service.sync(embed=False) == 0
    assert indexed == [(str(note.metadata.id), False)]

    class Store:
        def __init__(self) -> None:
            self.rebuilt = 0

        def rebuild(self, notes) -> None:
            self.rebuilt = len(list(notes))

        def close(self) -> None:
            return None

    store = Store()
    monkeypatch.setattr(service, "_graph_store", lambda: store)
    assert service.rebuild() == 1
    assert store.rebuilt == 1


def test_service_lifecycle_wrappers_and_failure_fallbacks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Thin orchestration methods preserve retries, delegation, and safe errors."""
    service = BrainService(make_settings(tmp_path / "brain"))

    class Store:
        def __init__(self) -> None:
            self.closed = False

        def ensure_schema(self, **kwargs: object) -> None:
            return None

        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def upsert_note(self, note, embedding=None) -> None:
            return None

    store = Store()
    monkeypatch.setattr(service, "_graph_store", lambda: store)
    service.initialize()
    assert service.doctor().gateway == "unavailable"

    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Wrapper note", space_id="work"),
        "summary",
    )

    class Ingestor:
        def ingest(self, record, extract=True, **kwargs):
            note.metadata.title = record.title
            return SimpleNamespace(
                source_id=record.source_id,
                note_id=str(note.metadata.id),
                status="promoted",
            )

        def promote(self, source_id):
            return note

        def reject(self, source_id):
            return None

    monkeypatch.setattr(service, "_ingestor", lambda: Ingestor())
    result = service.ingest(
        SourceRecord(
            source_id="source",
            title="Source",
            content="content",
            space_id="work",
            locator="memory://source",
        ),
        extract=False,
    )
    assert result.source_id == "source"
    assert service.promote("source", index=False) is note
    service.reject("source")

    def index_offline(current, embed):
        raise RuntimeError("offline")

        monkeypatch.setattr(service, "index_note", index_offline)
        remembered = service.remember("remembered", "", "work")
        assert remembered is note
    with pytest.raises(ValueError):
        service.notes_by_date(date(2026, 8, 2), date(2026, 8, 1))
    assert service.list_by_label([]) == []

    alias_count = service._store_aliases(
        ReflectionKnowledge(
            aliases=[
                LabelAliasProposal(
                    alias="tf",
                    canonical="technology:terraform",
                    confidence=0.95,
                ),
                LabelAliasProposal(
                    alias="weak",
                    canonical="topic:weak",
                    confidence=0.5,
                ),
            ]
        )
    )
    assert alias_count == 1


def test_service_index_note_and_incremental_embedding_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Embedding failures preserve lexical indexability and record pending state."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Embedding note", space_id="work"),
        "summary",
    )
    indexed_embeddings: list[object] = []

    class SyncStore:
        def ensure_schema(self, **kwargs: object) -> None:
            assert kwargs["embedding_dimensions"] is None

        def upsert_notes(self, rows) -> None:
            indexed_embeddings.extend(embedding for _, embedding in rows)

        def close(self) -> None:
            return None

    service.gateway.embed_batch = lambda texts: (_ for _ in ()).throw(
        RuntimeError("embedding unavailable")
    )
    monkeypatch.setattr(service, "_graph_store", lambda: SyncStore())
    assert service.sync(embed=True) == 1
    assert indexed_embeddings == [None]
    assert "embedding-pending" in (
        service.settings.state_dir / "index-state.json"
    ).read_text(encoding="utf-8")

    class Store:
        def ensure_schema(self, **kwargs: object) -> None:
            assert kwargs["embedding_dimensions"] == 2

        def upsert_note(self, current, embedding=None) -> None:
            assert embedding == [0.1, 0.2]

        def close(self) -> None:
            return None

    service.gateway.embed = lambda text: [0.1, 0.2]
    monkeypatch.setattr(service, "_graph_store", lambda: Store())
    monkeypatch.setattr(
        service,
        "index_note",
        BrainService.index_note.__get__(service),
    )
    service.index_note(note, embed=True)
    assert note.metadata.embedding_model == service.settings.embedding_model
