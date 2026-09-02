"""Tests for broad retrieval with conservative Brain output semantics."""

from __future__ import annotations

from pathlib import Path

from exocortex.models import (
    ActionSignature,
    Claim,
    EvidenceSpan,
    KnowledgeScope,
    NoteMetadata,
    SearchResult,
    VaultNote,
)
from exocortex.service import BrainService
from tests.conftest import make_settings


def _graph_result(note_id: str, score: float = 1.0) -> SearchResult:
    """Build one graph candidate for search orchestration tests."""
    return SearchResult(
        note_id=note_id,
        title="Search candidate",
        note_type="task",
        space_id="work",
        path="Vault/work/Tasks/search-candidate.md",
        score=score,
        excerpt="Candidate",
    )


class _Store:
    """Small graph double with configurable lexical and vector results."""

    def __init__(
        self,
        lexical: list[SearchResult],
        semantic: list[SearchResult],
        lexical_error: bool = False,
    ) -> None:
        self.lexical = lexical
        self.semantic = semantic
        self.lexical_error = lexical_error

    def search_fulltext(self, query, space_id, limit):
        if self.lexical_error:
            raise RuntimeError("lexical unavailable")
        return self.lexical

    def search_vector(self, embedding, space_id, limit):
        return self.semantic

    def close(self) -> None:
        return None


def test_hybrid_search_loads_ranked_notes_without_repeated_get_scans(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Search verifies many candidates through one bulk Vault lookup."""
    service = BrainService(make_settings(tmp_path / "brain"))
    results = []
    for index in range(20):
        note = service.vault.upsert_managed(
            NoteMetadata(
                type="task",
                title=f"Deployment task {index}",
                space_id="work",
            ),
            f"## Summary\nDeployment task {index}.",
        )
        results.append(_graph_result(str(note.metadata.id), 1.0 - index / 100))

    monkeypatch.setattr(service, "_graph_store", lambda: _Store(results, []))
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )
    monkeypatch.setattr(
        service.vault,
        "get",
        lambda note_id: (_ for _ in ()).throw(
            AssertionError(f"per-note lookup used: {note_id}")
        ),
    )

    found, degraded = service._hybrid_search("deployment", None, 50)

    assert degraded is True
    assert len(found) == 20


def test_empty_canonical_content_is_not_searchable_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Metadata-only notes are filtered before search results are returned."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = VaultNote(
        metadata=NoteMetadata(type="task", title="Missing content", space_id="work"),
        content="",
        path="Vault/work/Tasks/missing-content.md",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([_graph_result(str(note.metadata.id))], []),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )
    monkeypatch.setattr(
        service.vault,
        "get_many",
        lambda note_ids: {str(note.metadata.id): note},
    )

    found, degraded = service._hybrid_search("missing content", None, 10)

    assert found == []
    assert degraded is True


def test_action_context_does_not_confirm_dataset_deletion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A dataset mention and delete_file flag remain context-only evidence."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Default dataset context",
            space_id="work",
        ),
        "## Summary\nThe default dataset is ssim_files_source.\n"
        "Terraform uses delete_file=true for files.",
    )
    result = _graph_result(str(note.metadata.id))
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([result], []),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response("delete default dataset")

    assert response.status == "abstained"
    assert response.data == []
    related = response.meta["context_related"]
    assert related[0]["claim_support"] == "context_only"
    assert "default dataset" in related[0]["excerpt"]
    assert "insufficient_direct_action_evidence" == response.meta["abstention_reason"]


def test_literal_anchors_recover_context_when_graph_misses_note(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Literal entity/object anchors preserve recall without confirming action."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Itinerary Datapointers dataset disablement process",
            space_id="work",
            labels=["topic:itinerary-datapointers", "topic:dataset-management"],
        ),
        "## Summary\nThe default dataset is configured in Terraform for the "
        "itinerary-datapointers infrastructure repository.",
    )
    monkeypatch.setattr(service, "_graph_store", lambda: _Store([], []))
    monkeypatch.setattr(service.gateway, "embed", lambda *args, **kwargs: [0.1])

    response = service.search_response(
        "¿Cómo eliminamos el dataset por defecto en itinerary-datapointers?"
    )

    assert response.status == "abstained"
    assert response.data == []
    assert response.meta["abstention_reason"] == (
        "insufficient_direct_action_evidence"
    )
    assert response.meta["context_related"][0]["note_id"] == str(note.metadata.id)
    assert response.meta["context_related"][0]["claim_support"] == "context_only"


def test_confirmed_action_is_returned_as_direct_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An evidence-backed action and object can support a direct answer."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Dataset cleanup",
            space_id="work",
            actions=[
                ActionSignature(
                    action_key="delete",
                    canonical_action_key="dataset.delete",
                    objects=["dataset"],
                    outcome="confirmed_success",
                )
            ],
            claims=[
                Claim(
                    id="claim-delete",
                    text="The default dataset was deleted successfully.",
                    claim_key="dataset.delete",
                    evidence=[
                        EvidenceSpan(
                            source_id="source-delete",
                            fragment="dataset was deleted",
                            precision="exact",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nThe default dataset was deleted successfully.",
    )
    result = _graph_result(str(note.metadata.id))
    monkeypatch.setattr(service, "_graph_store", lambda: _Store([result], []))
    monkeypatch.setattr(service.gateway, "embed", lambda *args, **kwargs: [0.1])

    response = service.search_response("Did we delete the default dataset?")

    assert response.status == "ok"
    assert response.data[0]["claim_support"] == "direct"
    assert response.data[0]["verification_status"] == "verified"
    assert response.data[0]["match_reasons"]


def test_degraded_search_drops_semantic_only_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A degraded lexical path never surfaces vector-only evidence."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Unrelated note", space_id="work"),
        "## Summary\nUnrelated historical context.",
    )
    semantic = _graph_result(str(note.metadata.id))
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([], [semantic], lexical_error=True),
    )
    monkeypatch.setattr(service.gateway, "embed", lambda *args, **kwargs: [0.1])

    response = service.search_response("quantum annealing recipe")

    assert response.status == "abstained"
    assert response.data == []
    assert response.meta["embedding_degraded"] is True
    assert response.meta["related_candidates"] == []


def test_exploration_mode_exposes_semantic_candidates_separately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exploration preserves recall without mixing candidates into facts."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Historical context", space_id="work"),
        "## Summary\nA related historical note.",
    )
    semantic = _graph_result(str(note.metadata.id))
    monkeypatch.setattr(service, "_graph_store", lambda: _Store([], [semantic]))
    monkeypatch.setattr(service.gateway, "embed", lambda *args, **kwargs: [0.1])

    response = service.search_response(
        "what may be related to deployment history",
        answer_mode="exploration",
    )

    assert response.data == []
    assert response.meta["related_candidates"][0]["verification_status"] == "candidate"
    assert response.meta["answer_mode"] == "exploration"


def test_explicit_repository_scope_excludes_other_notes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An explicit repository filter is a hard scope constraint."""
    service = BrainService(make_settings(tmp_path / "brain"))
    matching = service.vault.upsert_managed(
        NoteMetadata(
            type="repository",
            title="Videowall repository",
            space_id="work",
            labels=["repository:itinerary-videowall"],
        ),
        "## Summary\nRepository itinerary-videowall.",
    )
    other = service.vault.upsert_managed(
        NoteMetadata(
            type="repository",
            title="Other repository",
            space_id="work",
            labels=["repository:other"],
        ),
        "## Summary\nRepository other.",
    )
    results = [
        _graph_result(str(matching.metadata.id)),
        _graph_result(str(other.metadata.id)),
    ]
    monkeypatch.setattr(service, "_graph_store", lambda: _Store(results, []))
    monkeypatch.setattr(service.gateway, "embed", lambda *args, **kwargs: [0.1])

    found = service.search(
        "repository history",
        repository_id="itinerary-videowall",
    )

    assert [result.note_id for result in found] == [str(matching.metadata.id)]


def test_explicit_service_keeps_related_adapter_as_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A reusable adapter survives a service mismatch only as context."""
    service = BrainService(make_settings(tmp_path / "brain"))
    adapter = service.vault.upsert_managed(
        NoteMetadata(
            type="pattern",
            title="Python DBI base image catalog",
            space_id="work",
            labels=["technology:python", "technology:docker", "topic:gcp"],
            knowledge_level="adapter",
            scope=KnowledgeScope(
                organization="Acme Corp",
                provider="GCP",
                runtime="Python",
                confidence=0.95,
            ),
        ),
        "## Summary\nActualizar el Dockerfile para usar una imagen base "
        "Python DBI de Artifact Registry, preferentemente la variante slim.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([], []),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response(
        "En el contexto de GCP de Acme Corp, ¿qué documentación "
        "indica cómo actualizar la imagen base de Python para un servicio "
        "Cloud Run como ssim-itinerary-validation, qué imagen/tag GCP "
        "se debe usar en lugar de advana-dbi-base-python3.10-slim-bullseye?"
    )

    assert response.status == "abstained"
    assert response.data == []
    related = response.meta["context_related"]
    assert related[0]["note_id"] == str(adapter.metadata.id)
    assert related[0]["claim_support"] == "context_only"
    assert related[0]["scope_match"] == "mismatch"


def test_explicit_service_still_excludes_unrelated_concrete_note(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Concrete notes from another service remain filtered out."""
    service = BrainService(make_settings(tmp_path / "brain"))
    unrelated = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="SFTP image update",
            space_id="work",
            labels=["technology:docker", "topic:sftp"],
        ),
        "## Summary\nActualizar la imagen del servicio SFTP.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([_graph_result(str(unrelated.metadata.id))], []),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response(
        "¿Cómo actualizar la imagen base Python de ssim-itinerary-validation?"
    )

    assert response.status == "abstained"
    assert response.data == []
    assert response.meta["context_related"] == []


def test_mismatched_adapter_cannot_promote_direct_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A scope-mismatched direct claim cannot become answer data."""
    service = BrainService(make_settings(tmp_path / "brain"))
    adapter = service.vault.upsert_managed(
        NoteMetadata(
            type="pattern",
            title="Python DBI base image catalog",
            space_id="work",
            labels=["technology:python", "technology:docker", "topic:gcp"],
            knowledge_level="adapter",
            actions=[
                ActionSignature(
                    action_key="actualizar",
                    canonical_action_key="replace-product-dockerfile-base-image",
                    objects=["image"],
                    outcome="confirmed_success",
                )
            ],
            claims=[
                Claim(
                    id="claim-image-update",
                    text="The Python base image was updated successfully.",
                    claim_key="python-base-image-updated",
                    evidence=[
                        EvidenceSpan(
                            source_id="source-image-update",
                            fragment="base image was updated",
                            precision="exact",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nActualizar la imagen base Python del Dockerfile.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([_graph_result(str(adapter.metadata.id))], []),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response(
        "¿Actualizar la imagen de ssim-itinerary-validation?"
    )

    assert response.status == "abstained"
    assert response.data == []
    assert response.meta["abstention_reason"] == (
        "insufficient_direct_action_evidence"
    )


def test_search_feedback_is_sanitized_and_persisted(tmp_path: Path) -> None:
    """Explicit relevance feedback is stored as a local JSONL event."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Feedback target", space_id="work"),
        "## Summary\nTarget note.",
    )

    response = service.record_search_feedback(
        query="delete dataset",
        note_ids=[str(note.metadata.id)],
        relevance="irrelevant",
        reason="Raspberry Pi note was unrelated.",
        tags=["scope_mismatch", "too_specific"],
    )

    assert response.status == "stored"
    feedback_path = tmp_path / "brain" / "feedback" / "search.jsonl"
    assert feedback_path.exists()
    assert "Raspberry Pi note was unrelated." in feedback_path.read_text()
    assert "scope_mismatch" in feedback_path.read_text()


def test_generic_query_prefers_pattern_over_scoped_adapter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Generic questions prefer reusable knowledge without hiding adapters."""
    service = BrainService(make_settings(tmp_path / "brain"))
    pattern = service.vault.upsert_managed(
        NoteMetadata(
            type="pattern",
            title="OpenAI-compatible gateway integration",
            space_id="work",
            knowledge_level="pattern",
            pattern_key="openai-compatible-llm-gateway-integration",
        ),
        "## Summary\nUse a configurable OpenAI-compatible client behind a gateway.",
    )
    adapter = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Gateway on Cloud Run",
            space_id="work",
            knowledge_level="adapter",
            pattern_key="openai-compatible-llm-gateway-integration",
            scope=KnowledgeScope(
                organization="Acme Corp",
                provider="Cloud Run",
                runtime="Cloud Run",
                confidence=0.9,
            ),
        ),
        "## Summary\nUse the Cloud Run gateway from Cloud Run.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store(
            [
                _graph_result(str(pattern.metadata.id), score=1.0),
                _graph_result(str(adapter.metadata.id), score=0.99),
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response("llm gateway integration")

    assert response.data[0]["knowledge_level"] == "pattern"
    assert response.meta["query_scope"]["organization"] == ""
    assert response.meta["related_candidates"] == []


def test_acme_query_targets_organization_scope(tmp_path: Path, monkeypatch) -> None:
    """The explicit local alias scopes a query to Acme Corp."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Acme gateway deployment",
            space_id="work",
            knowledge_level="adapter",
            scope=KnowledgeScope(
                organization="Acme Corp",
                runtime="Cloud Run",
                confidence=0.9,
            ),
        ),
        "## Summary\nDeploy the gateway on Cloud Run for Acme Corp.",
    )
    result = _graph_result(str(note.metadata.id))
    monkeypatch.setattr(service, "_graph_store", lambda: _Store([result], []))
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response("gateway for acme on cloud run")

    assert response.meta["query_scope"]["organization"] == "acme-corp"
    assert response.meta["query_scope"]["runtime"] == "cloud-run"
    assert response.data[0]["scope_match"] == "explicit"


def test_generic_query_falls_back_to_specific_examples_when_pattern_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A scoped adapter is returned transparently when no pattern exists."""
    service = BrainService(make_settings(tmp_path / "brain"))
    adapter = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Codeen gateway integration",
            space_id="work",
            knowledge_level="adapter",
            pattern_key="openai-compatible-llm-gateway-integration",
        ),
        "## Summary\nUse an OpenAI-compatible client through the Codeen local "
        "gateway for the agent.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store([_graph_result(str(adapter.metadata.id))], []),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response("OpenAI-compatible LLM gateway integration")

    assert response.status == "degraded"
    assert response.data[0]["knowledge_level"] == "adapter"
    assert response.meta["pattern_gap"] is True
    assert response.meta["fallback_abstraction"] == ["adapter"]


def test_generic_query_does_not_promote_adjacent_pattern(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A semantically adjacent pattern remains context for a gateway query."""
    service = BrainService(make_settings(tmp_path / "brain"))
    adjacent = service.vault.upsert_managed(
        NoteMetadata(
            type="project",
            title="LLMOps inference platform",
            space_id="work",
            knowledge_level="pattern",
            pattern_key="layered-llm-inference-platform-repository",
        ),
        "## Summary\nA layered LLM inference platform repository on GKE "
        "serves applications through an API.",
    )
    result = _graph_result(str(adjacent.metadata.id))
    monkeypatch.setattr(service, "_graph_store", lambda: _Store([result], []))
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response("LLM gateway provider adapter pattern")

    assert response.status == "abstained"
    assert response.data == []
    assert response.meta["pattern_gap"] is True
    assert response.meta["context_related"][0]["pattern_key"] == (
        "layered-llm-inference-platform-repository"
    )


def test_generic_context_filters_unrelated_gateway_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Generic gateway context excludes results sharing only a generic word."""
    service = BrainService(make_settings(tmp_path / "brain"))
    codeen = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Codeen LLM gateway adapter",
            space_id="work",
            knowledge_level="example",
        ),
        "## Summary\nAn OpenAI-compatible LLM gateway adapter uses Codeen.",
    )
    sftp = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="SFTP gateway integration",
            space_id="work",
            knowledge_level="example",
        ),
        "## Summary\nAn SFTP gateway transfers files.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store(
                [
                    _graph_result(str(codeen.metadata.id)),
                    _graph_result(str(sftp.metadata.id), score=0.99),
                ],
                [],
            ),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response("LLM gateway provider adapter pattern")

    assert response.status == "abstained"
    context_titles = [item["title"] for item in response.meta["context_related"]]
    assert "Codeen LLM gateway adapter" in context_titles
    assert "SFTP gateway integration" not in context_titles


def test_generic_pattern_requires_matching_technical_anchor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A Terraform query does not promote a pattern from another technology."""
    service = BrainService(make_settings(tmp_path / "brain"))
    terraform_pattern = service.vault.upsert_managed(
        NoteMetadata(
            type="pattern",
            title="Terraform registry module migration",
            space_id="work",
            knowledge_level="pattern",
            pattern_key="terraform-registry-module-migration",
            labels=["technology:terraform", "topic:module-registry"],
        ),
        "## Summary\nMigrate standalone Terraform resources to registry modules.",
    )
    skills_pattern = service.vault.upsert_managed(
        NoteMetadata(
            type="pattern",
            title="Diseño de skills all-in-one con modularidad interna",
            space_id="work",
            knowledge_level="pattern",
            pattern_key="single-entrypoint-modular-orchestration",
            labels=["technology:cloud-run", "topic:skills"],
        ),
        "## Summary\nUse one skill entrypoint with modular internal flows.",
    )
    monkeypatch.setattr(
        service,
        "_graph_store",
        lambda: _Store(
            [
                _graph_result(str(skills_pattern.metadata.id), score=1.0),
                _graph_result(str(terraform_pattern.metadata.id), score=0.8),
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: [0.1],
    )

    response = service.search_response(
        "¿Qué patrón usamos para migrar recursos Terraform standalone a "
        "módulos de registry?"
    )

    assert response.status == "ok"
    assert response.data[0]["title"] == "Terraform registry module migration"
    assert all(
        item["title"] != "Diseño de skills all-in-one con modularidad interna"
        for item in response.data
    )
    assert all(
        item["title"] != "Diseño de skills all-in-one con modularidad interna"
        for item in response.meta["context_related"]
    )


def test_scoped_abstention_has_explicit_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Missing evidence for an explicit provider is named in the envelope."""
    service = BrainService(make_settings(tmp_path / "brain"))
    monkeypatch.setattr(service, "_graph_store", lambda: _Store([], []))
    monkeypatch.setattr(
        service.gateway,
        "embed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("degraded")),
    )

    response = service.search_response(
        "¿Qué implementamos para Azure OpenAI en eu-west-1?"
    )

    assert response.status == "abstained"
    assert response.meta["abstention_reason"] == "no_evidence_for_scope"
