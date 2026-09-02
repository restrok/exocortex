"""Neo4j projection and retrieval for canonical Vault notes."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from neo4j import GraphDatabase

from exocortex.config import Settings
from exocortex.models import (
    ActionSignature,
    Claim,
    KnowledgeScope,
    SearchResult,
    SourceReference,
    VaultNote,
)
from exocortex.vault import extract_links

_LABELS = {
    "project": "Project",
    "task": "Task",
    "decision": "Decision",
    "pattern": "Pattern",
    "incident": "Incident",
    "command": "Command",
    "repository": "Repository",
    "system": "System",
    "workflow": "Workflow",
}


class Neo4jStore:
    """Project canonical notes into Neo4j and run graph-aware retrieval."""

    def __init__(self, settings: Settings) -> None:
        """Create a Neo4j driver from the configured connection values."""
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(
                settings.neo4j_username,
                settings.neo4j_password.get_secret_value(),
            ),
        )

    def close(self) -> None:
        """Close the underlying Neo4j driver."""
        self._driver.close()

    def verify_connectivity(self) -> None:
        """Raise when Neo4j cannot be reached."""
        self._driver.verify_connectivity()

    def ensure_schema(self, embedding_dimensions: int | None = None) -> None:
        """Create idempotent constraints and indexes."""
        statements = [
            (
                "CREATE CONSTRAINT note_id IF NOT EXISTS "
                "FOR (note:Note) REQUIRE note.id IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT source_id IF NOT EXISTS "
                "FOR (source:Source) REQUIRE source.id IS UNIQUE"
            ),
            (
                "CREATE FULLTEXT INDEX note_search_v2 IF NOT EXISTS "
                "FOR (note:Note) ON EACH [note.title, note.content, note.label_text]"
            ),
            (
                "CREATE CONSTRAINT label_id IF NOT EXISTS "
                "FOR (label:Label) REQUIRE label.id IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT claim_id IF NOT EXISTS "
                "FOR (claim:Claim) REQUIRE claim.id IS UNIQUE"
            ),
        ]
        if embedding_dimensions:
            statements.append(
                "CREATE VECTOR INDEX note_embedding IF NOT EXISTS "
                "FOR (note:Note) ON (note.embedding) OPTIONS {indexConfig: {"
                f"`vector.dimensions`: {embedding_dimensions}, "
                "`vector.similarity_function`: 'cosine'}}"
            )
        with self._driver.session() as session:
            for statement in statements:
                session.run(statement).consume()
            session.run(
                "MATCH (note:Note)-[legacy]->(linked:Note) "
                "WHERE type(legacy) = 'MENTIONS' "
                "MERGE (note)-[:LINKS_TO]->(linked) "
                "DELETE legacy"
            ).consume()

    def upsert_note(
        self,
        note: VaultNote,
        embedding: list[float] | None = None,
    ) -> None:
        """Upsert a note, source provenance, and explicit note links."""
        self.upsert_notes([(note, embedding)])

    def upsert_notes(
        self,
        notes: list[tuple[VaultNote, list[float] | None]],
    ) -> None:
        """Upsert notes and relationships using bounded UNWIND operations."""
        if not notes:
            return
        rows = [_note_row(note, embedding) for note, embedding in notes]
        with self._driver.session() as session:
            for note_type, label in _LABELS.items():
                typed_rows = [row for row in rows if row["note_type"] == note_type]
                if not typed_rows:
                    continue
                session.run(
                    "UNWIND $notes AS row "
                    "MERGE (note:Note {id: row.id}) "
                    "REMOVE note:Project:Task:Decision:Pattern:Incident:Command:"
                    "Repository:System:Workflow "
                    f"SET note:{label} "
                    "SET note += row.properties",
                    notes=typed_rows,
                ).consume()
            session.run(
                "UNWIND $notes AS row "
                "MATCH (note:Note {id: row.id}) "
                "OPTIONAL MATCH (note)-[relationship]->() "
                "WHERE type(relationship) IN $managed_relationships "
                "DELETE relationship",
                notes=rows,
                managed_relationships=[
                    "ASSERTS",
                    "DERIVED_FROM",
                    "LINKS_TO",
                    "MENTIONS",
                    "TAGGED_WITH",
                ],
            ).consume()
            session.run(
                "UNWIND $notes AS row "
                "MATCH (note:Note {id: row.id}) "
                "UNWIND row.labels AS label_id "
                "MERGE (label:Label {id: label_id}) "
                "MERGE (note)-[:TAGGED_WITH]->(label)",
                notes=rows,
            ).consume()
            session.run(
                "UNWIND $notes AS row "
                "MATCH (note:Note {id: row.id}) "
                "UNWIND row.sources AS source "
                "MERGE (source_node:Source {id: source.id}) "
                "SET source_node.locator = source.locator, "
                "source_node.content_hash = source.content_hash, "
                "source_node.occurred_on = source.occurred_on, "
                "source_node.session_id = source.session_id, "
                "source_node.segment_id = source.segment_id, "
                "source_node.event_start = source.event_start, "
                "source_node.event_end = source.event_end "
                "MERGE (note)-[:DERIVED_FROM]->(source_node)",
                notes=rows,
            ).consume()
            session.run(
                "UNWIND $notes AS row "
                "MATCH (note:Note {id: row.id}) "
                "UNWIND row.links AS link_reference "
                "MATCH (linked:Note) "
                "WHERE linked.space_id = row.space_id "
                "AND (linked.id = link_reference "
                "OR linked.title = link_reference "
                "OR linked.path = link_reference "
                "OR linked.pattern_key = link_reference) "
                "MERGE (note)-[:LINKS_TO]->(linked)",
                notes=rows,
            ).consume()
            session.run(
                "UNWIND $notes AS row "
                "MATCH (note:Note {id: row.id}) "
                "UNWIND row.claims AS claim_data "
                "MERGE (claim:Claim {id: claim_data.id}) "
                "SET claim += claim_data.properties "
                "MERGE (note)-[:ASSERTS]->(claim)",
                notes=rows,
            ).consume()
            session.run(
                "UNWIND $notes AS row "
                "UNWIND row.claims AS claim_data "
                "MATCH (claim:Claim {id: claim_data.id}) "
                "UNWIND claim_data.evidence AS evidence "
                "MERGE (source:Source {id: evidence.source_id}) "
                "ON CREATE SET source.locator = evidence.locator, "
                "source.content_hash = evidence.content_hash, "
                "source.session_id = evidence.session_id, "
                "source.segment_id = evidence.segment_id, "
                "source.event_start = evidence.event_start, "
                "source.event_end = evidence.event_end "
                "MERGE (claim)-[:SUPPORTED_BY]->(source)",
                notes=rows,
            ).consume()
            claim_keys = sorted(
                {
                    claim_data["properties"]["claim_key"]
                    for row in rows
                    for claim_data in row["claims"]
                }
            )
            session.run(
                "MATCH (claim:Claim)-[relationship]-() "
                "WHERE claim.claim_key IN $claim_keys "
                "AND type(relationship) = 'CONTRADICTS' "
                "WITH DISTINCT relationship "
                "DELETE relationship",
                claim_keys=claim_keys,
            ).consume()
            session.run(
                "MATCH (claim:Claim) "
                "WHERE claim.claim_key IN $claim_keys "
                "WITH claim.claim_key AS claim_key, collect(claim) AS claims "
                "UNWIND claims AS claim "
                "UNWIND claims AS other "
                "WITH claim, other "
                "WHERE claim.id < other.id AND claim.polarity <> other.polarity "
                "MERGE (claim)-[:CONTRADICTS]->(other)",
                claim_keys=claim_keys,
            ).consume()
            session.run(
                "UNWIND $notes AS row "
                "MATCH (workflow:Note {id: row.id}) "
                "UNWIND row.workflow_step_claim_ids AS claim_id "
                "MATCH (claim:Claim {id: claim_id}) "
                "MERGE (claim)-[:JUSTIFIES_STEP]->(workflow)",
                notes=rows,
            ).consume()

    def rebuild(self, notes: Iterable[VaultNote]) -> None:
        """Replace the graph projection from canonical notes."""
        with self._driver.session() as session:
            session.run("MATCH (node) DETACH DELETE node").consume()
        self.ensure_schema()
        note_list = list(notes)
        for offset in range(0, len(note_list), 100):
            self.upsert_notes(
                [(note, None) for note in note_list[offset : offset + 100]]
            )

    def search_fulltext(
        self,
        query: str,
        space_id: str | None,
        limit: int,
    ) -> list[SearchResult]:
        """Search canonical note text through Neo4j full-text indexing."""
        records = self._run_search(
            (
                "CALL db.index.fulltext.queryNodes('note_search_v2', $search_query) "
                "YIELD node, score "
                "WHERE node.superseded_by IS NULL AND "
                "($space_id IS NULL OR node.space_id = $space_id) "
                "OPTIONAL MATCH (node)-[:DERIVED_FROM]->(source:Source) "
                "OPTIONAL MATCH (node)-[:LINKS_TO|TAGGED_WITH]->(related) "
                "RETURN node, score, collect(DISTINCT source) AS sources, "
                "count(DISTINCT related) AS graph_score "
                "ORDER BY score DESC LIMIT $limit"
            ),
            search_query=query,
            space_id=space_id,
            limit=limit,
        )
        return [_result_from_record(record, lexical=True) for record in records]

    def search_vector(
        self,
        embedding: list[float],
        space_id: str | None,
        limit: int,
    ) -> list[SearchResult]:
        """Search canonical note embeddings through Neo4j vector indexing."""
        records = self._run_search(
            (
                "CALL db.index.vector.queryNodes('note_embedding', $limit, $embedding) "
                "YIELD node, score "
                "WHERE node.superseded_by IS NULL AND "
                "($space_id IS NULL OR node.space_id = $space_id) "
                "OPTIONAL MATCH (node)-[:DERIVED_FROM]->(source:Source) "
                "OPTIONAL MATCH (node)-[:LINKS_TO|TAGGED_WITH]->(related) "
                "RETURN node, score, collect(DISTINCT source) AS sources, "
                "count(DISTINCT related) AS graph_score "
                "ORDER BY score DESC"
            ),
            embedding=embedding,
            space_id=space_id,
            limit=limit,
        )
        return [_result_from_record(record, semantic=True) for record in records]

    def get(self, note_id: str) -> SearchResult | None:
        """Return one projected note by its canonical identifier."""
        records = self._run_search(
            "MATCH (node:Note {id: $note_id}) "
            "OPTIONAL MATCH (node)-[:DERIVED_FROM]->(source:Source) "
            "RETURN node, 1.0 AS score, collect(source) AS sources",
            note_id=note_id,
        )
        return _result_from_record(records[0]) if records else None

    def _run_search(self, statement: str, **parameters: Any) -> list[dict[str, Any]]:
        """Run a read transaction and return serializable record dictionaries."""
        with self._driver.session() as session:
            result = session.run(statement, **parameters)
            return [record.data() for record in result]


def _note_row(
    note: VaultNote,
    embedding: list[float] | None,
) -> dict[str, Any]:
    """Serialize one note for a parameterized Neo4j batch upsert."""
    labels = sorted(set(note.metadata.labels + note.metadata.manual_labels))
    properties: dict[str, Any] = {
        "schema_version": note.metadata.schema_version,
        "id": str(note.metadata.id),
        "title": note.metadata.title,
        "note_type": note.metadata.type,
        "space_id": note.metadata.space_id,
        "path": note.path,
        "content": note.content,
        "confidence": note.metadata.confidence,
        "usage_count": note.metadata.usage_count,
        "success_count": note.metadata.success_count,
        "last_feedback_at": (
            note.metadata.last_feedback_at.isoformat()
            if note.metadata.last_feedback_at
            else None
        ),
        "last_feedback_notes": note.metadata.last_feedback_notes,
        "updated_at": note.metadata.updated_at.isoformat(),
        "labels": labels,
        "label_text": " ".join(labels),
        "evidence_status": note.metadata.evidence_status,
        "superseded_by": note.metadata.superseded_by,
        "recommendation_state": note.metadata.recommendation_state,
        "prompt_version": note.metadata.prompt_version,
        "model_version": note.metadata.model_version,
        "extraction_status": note.metadata.extraction_status,
        "embedding_model": note.metadata.embedding_model,
        "knowledge_level": note.metadata.knowledge_level,
        "pattern_key": note.metadata.pattern_key,
        "scope_json": note.metadata.scope.model_dump_json(),
        "claims_json": json.dumps(
            [claim.model_dump(mode="json") for claim in note.metadata.claims],
            sort_keys=True,
        ),
        "actions_json": json.dumps(
            [action.model_dump(mode="json") for action in note.metadata.actions],
            sort_keys=True,
        ),
    }
    if embedding is not None:
        properties["embedding"] = embedding
    claims = [
        {
            "id": claim.id,
            "properties": {
                "text": claim.text,
                "claim_key": claim.claim_key,
                "polarity": claim.polarity,
                "claim_type": claim.claim_type,
                "confidence": claim.confidence,
            },
            "evidence": [
                {
                    "source_id": evidence.source_id,
                    "locator": evidence.source_id,
                    "content_hash": evidence.fragment_hash,
                    "session_id": evidence.session_id,
                    "segment_id": evidence.segment_id,
                    "event_start": evidence.event_start,
                    "event_end": evidence.event_end,
                }
                for evidence in claim.evidence
            ],
        }
        for claim in note.metadata.claims
    ]
    return {
        "id": str(note.metadata.id),
        "note_type": note.metadata.type,
        "properties": properties,
        "labels": labels,
        "sources": [
            reference.model_dump(mode="json") for reference in note.metadata.source_refs
        ],
        "links": extract_links(note),
        "space_id": note.metadata.space_id,
        "claims": claims,
        "workflow_step_claim_ids": [
            claim_id
            for step in note.metadata.workflow_steps
            for claim_id in step.get("evidence_claim_ids", [])
        ],
    }


def _result_from_record(
    record: dict[str, Any],
    lexical: bool = False,
    semantic: bool = False,
) -> SearchResult:
    """Convert a Neo4j result into the common grounded search model."""
    node = dict(record["node"])
    content = str(node.get("content", ""))
    try:
        claims = [
            Claim.model_validate(item)
            for item in json.loads(str(node.get("claims_json", "[]")))
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        claims = []
    try:
        actions = [
            ActionSignature.model_validate(item)
            for item in json.loads(str(node.get("actions_json", "[]")))
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        actions = []
    try:
        scope = KnowledgeScope.model_validate(
            json.loads(str(node.get("scope_json", "{}")))
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        scope = KnowledgeScope()
    score = float(record["score"])
    return SearchResult(
        note_id=str(node["id"]),
        title=str(node.get("title", "")),
        note_type=str(node.get("note_type", "task")),
        space_id=str(node.get("space_id", "")),
        path=str(node.get("path", "")),
        score=score,
        excerpt=content[:500],
        source_refs=[
            SourceReference.model_validate(dict(source))
            for source in record.get("sources", [])
            if source is not None
        ],
        labels=[str(value) for value in node.get("labels", [])],
        evidence_status=str(node.get("evidence_status", "unknown")),
        recommendation_state=str(node.get("recommendation_state", "active")),
        confidence=float(node.get("confidence", 0.0)),
        recommendation_level=(
            "auto_apply"
            if str(node.get("note_type", "task")) == "workflow"
            and float(node.get("confidence", 0.0)) >= 0.80
            else (
                "confirm"
                if str(node.get("note_type", "task")) == "workflow"
                else None
            )
        ),
        lexical_score=score if lexical else 0.0,
        semantic_score=score if semantic else 0.0,
        graph_score=float(record.get("graph_score", 0.0)),
        claims=claims,
        actions=actions,
        knowledge_level=str(node.get("knowledge_level", "example")),
        pattern_key=str(node.get("pattern_key", "")),
        scope=scope,
    )
