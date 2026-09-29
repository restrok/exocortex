"""Tests for graph projection and result conversion without Neo4j."""

from pathlib import Path

from exocortex import graph
from exocortex.graph import _result_from_record
from exocortex.models import (
    Claim,
    EvidenceSpan,
    NoteMetadata,
    SourceReference,
    VaultNote,
)
from tests.conftest import make_settings


def test_graph_result_decodes_v2_claims_and_scores() -> None:
    """Projected metadata becomes a grounded search result."""
    record = {
        "node": {
            "id": "note-1",
            "title": "Terraform validation",
            "note_type": "task",
            "space_id": "work",
            "path": "Vault/work/Tasks/terraform.md",
            "content": "## Summary\nRun the plan.",
            "confidence": 0.9,
            "recommendation_state": "active",
            "evidence_status": "confirmed_success",
            "labels": ["technology:terraform"],
            "claims_json": (
                '[{"claim_type":"tool_observation",'
                '"claim_key":"terraform.plan", "confidence":0.9,'
                '"evidence":[], "id":"claim-1", "polarity":"affirmed",'
                '"text":"Run the plan."}]'
            ),
        },
        "score": 0.8,
        "sources": [],
    }

    result = _result_from_record(record, lexical=True)

    assert result.note_id == "note-1"
    assert result.lexical_score == 0.8
    assert result.claims[0].claim_type == "tool_observation"


def test_graph_store_projects_notes_and_runs_search_queries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Projection, provenance joins, and search paths use the expected driver API."""

    class Result:
        def __init__(self, records=None) -> None:
            self.records = records or []

        def consume(self) -> "Result":
            return self

        def __iter__(self):
            return iter(self.records)

    class Record:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def data(self) -> dict[str, object]:
            return self.payload

    class Session:
        def __init__(self, calls: list[tuple[str, dict[str, object]]]) -> None:
            self.calls = calls

        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def run(self, query: str, **parameters: object) -> Result:
            self.calls.append((query, parameters))
            if "fulltext" in query or "vector.queryNodes" in query:
                return Result([Record(_search_record())])
            if "MATCH (node:Note {id:" in query:
                return Result([Record(_search_record())])
            return Result()

    class Driver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def session(self) -> Session:
            return Session(self.calls)

        def close(self) -> None:
            self.calls.append(("close", {}))

        def verify_connectivity(self) -> None:
            self.calls.append(("verify", {}))

    driver = Driver()
    monkeypatch.setattr(graph.GraphDatabase, "driver", lambda *args, **kwargs: driver)
    service_settings = make_settings(tmp_path / "brain")
    store = graph.Neo4jStore(service_settings)
    note = VaultNote(
        metadata=NoteMetadata(
            type="task",
            title="Terraform validation",
            space_id="work",
            links=["Related note"],
            labels=["technology:terraform"],
            source_refs=[
                SourceReference(
                    id="source-1",
                    locator="session://one",
                    content_hash="hash",
                )
            ],
            claims=[
                Claim(
                    id="claim-1",
                    text="Run the plan.",
                    claim_key="terraform.plan",
                    claim_type="tool_observation",
                    evidence=[EvidenceSpan(source_id="source-1", fragment="plan")],
                )
            ],
            workflow_steps=[{"evidence_claim_ids": ["claim-1"]}],
        ),
        content="## Summary\nRun the plan.",
        path="Vault/work/Tasks/terraform-validation.md",
    )

    store.ensure_schema(embedding_dimensions=3)
    store.upsert_note(note, embedding=[0.1, 0.2, 0.3])
    store.verify_connectivity()
    lexical = store.search_fulltext("terraform", "work", 5)
    semantic = store.search_vector([0.1, 0.2, 0.3], "work", 5)
    assert store.get("note-1") is not None
    store.rebuild([note])
    store.close()

    assert lexical[0].lexical_score == 0.8
    assert semantic[0].semantic_score == 0.8
    vector_statements = [
        statement for statement, _ in driver.calls if "CREATE VECTOR INDEX" in statement
    ]
    assert any("`vector.dimensions`: 3" in statement for statement in vector_statements)
    assert any(
        "`vector.similarity_function`: 'cosine'" in statement
        for statement in vector_statements
    )
    assert len(driver.calls) > 15
    statements = "\n".join(statement for statement, _ in driver.calls)
    assert "UNWIND $notes AS row" in statements
    assert "MATCH (claim:Claim), (other:Claim)" not in statements
    assert "WITH claim.claim_key AS claim_key" in statements
    assert "[:LINKS_TO]" in statements
    assert "linked.pattern_key = link_reference" in statements
    assert "[relationship:MENTIONS]" not in statements


def _search_record() -> dict[str, object]:
    """Return one projected node for fake graph query results."""
    return {
        "node": {
            "id": "note-1",
            "title": "Terraform validation",
            "note_type": "task",
            "space_id": "work",
            "path": "note.md",
            "content": "Run the plan.",
            "confidence": 0.9,
            "recommendation_state": "active",
            "evidence_status": "confirmed_success",
            "labels": ["technology:terraform"],
            "claims_json": "[]",
        },
        "score": 0.8,
        "sources": [],
    }


def test_graph_search_and_links_compatible_spaces(tmp_path: Path, monkeypatch) -> None:
    """Graph queries filter by allowed_spaces and relax link compatibility."""

    class Result:
        def __init__(self, records=None) -> None:
            self.records = records or []

        def consume(self) -> "Result":
            return self

        def __iter__(self):
            return iter(self.records)

    class Record:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def data(self) -> dict[str, object]:
            return self.payload

    calls: list[tuple[str, dict[str, object]]] = []

    class Session:
        def __enter__(self) -> "Session":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def run(self, query: str, **parameters: object) -> Result:
            calls.append((query, parameters))
            if "fulltext" in query or "vector.queryNodes" in query:
                return Result([Record(_search_record())])
            return Result()

    class Driver:
        def session(self) -> Session:
            return Session()

        def close(self) -> None:
            return None

    monkeypatch.setattr(graph.GraphDatabase, "driver", lambda *a, **k: Driver())
    store = graph.Neo4jStore(make_settings(tmp_path / "brain"))

    # 1. Search fulltext with allowed_spaces
    store.search_fulltext("test query", allowed_spaces=["personal-fsirio", "shared"])
    fulltext_call = next(c for c in calls if "fulltext" in c[0])
    assert "node.space_id IN $allowed_spaces" in fulltext_call[0]
    assert fulltext_call[1]["allowed_spaces"] == ["personal-fsirio", "shared"]

    # 2. Search vector with allowed_spaces
    store.search_vector([0.1, 0.2], allowed_spaces=["personal-mercedes", "shared"])
    vector_call = next(c for c in calls if "vector.queryNodes" in c[0])
    assert "node.space_id IN $allowed_spaces" in vector_call[0]
    assert vector_call[1]["allowed_spaces"] == ["personal-mercedes", "shared"]

    # 3. Check link compatibility in upsert_notes
    note_fsirio = VaultNote(
        metadata=NoteMetadata(
            type="task",
            title="Fsirio Note",
            space_id="personal-fsirio",
            owner="fsirio",
            links=["Shared Note"],
        ),
        content="Link to shared",
        path="Vault/personal-fsirio/Tasks/fsirio.md",
    )
    store.upsert_notes([(note_fsirio, None)])
    link_call = next(c for c in calls if "[:LINKS_TO]" in c[0])
    # Must allow linking to or from shared, or fsirio to work
    assert "linked.space_id = 'shared'" in link_call[0]
    assert "row.space_id = 'shared'" in link_call[0]
    assert (
        "row.space_id = 'personal-fsirio' AND linked.space_id = 'work'" in link_call[0]
    )
