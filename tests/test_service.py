"""Tests for automatic canonical-memory storage."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from exocortex.models import (
    ActionSignature,
    Claim,
    EvidenceSpan,
    ExtractedKnowledge,
    NoteMetadata,
    ReflectionKnowledge,
    SearchResult,
    SourceReference,
    WorkflowProposal,
)
from exocortex.service import (
    BrainService,
    _effective_labels,
    _embedding_document,
    _evidence_claims,
    _excerpt,
    _fuse_results,
    _label_overlap,
    _normalize_workflow_text,
    _quality_factor,
    _reflection_action_groups,
    _reflection_cards,
    _related_experiences,
    _render_workflow,
    _safe_error,
    _search_result,
    _tokens,
    _unique_source_refs,
    _valid_workflow_evidence,
    _workflow_match,
)
from tests.conftest import make_settings


def test_extraction_canary_uses_short_dedicated_timeout(tmp_path: Path) -> None:
    """The canary fails fast without changing the normal extraction budget."""
    service = BrainService(make_settings(tmp_path / "brain"))

    class RecordingGateway:
        """Capture the canary timeout without making a network request."""

        def __init__(self) -> None:
            self.timeout_seconds: float | None = None

        def extract_batch(self, sources, timeout_seconds=None):
            del sources
            self.timeout_seconds = timeout_seconds
            return {"codex-brain-canary": object()}

    gateway = RecordingGateway()
    service.gateway = gateway

    result = service.extraction_canary()

    assert result["status"] == "passed"
    assert gateway.timeout_seconds == service.settings.canary_timeout_seconds


def test_remember_stores_a_sanitized_canonical_note(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Explicit memories are immediately available without review promotion."""
    service = BrainService(make_settings(tmp_path / "brain"))
    monkeypatch.setattr(service, "index_note", lambda note, embed: None)
    monkeypatch.setattr(
        service.gateway,
        "extract",
        lambda source: ExtractedKnowledge(
            title="Credential rotation",
            note_type="task",
            summary="Rotate credentials safely.",
            knowledge_level="example",
            evidence_status="decision",
        ),
    )

    note = service.remember(
        content="Use token sk-abcdefghijklmnopqrstuvwxyz123456 only in rotation.",
        title="Credential rotation",
        space_id="work",
    )

    assert note.path.startswith("Vault/work/Tasks/")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in note.content
    assert "## Source" in note.content
    assert service.get_note(str(note.metadata.id)) is not None


def test_remember_response_reports_structured_extraction_and_index_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Explicit memories expose extracted knowledge and degraded indexing."""
    service = BrainService(make_settings(tmp_path / "brain"))
    monkeypatch.setattr(
        service.gateway,
        "extract",
        lambda source: ExtractedKnowledge(
            title="Reusable validation pattern",
            note_type="pattern",
            summary="Validate before applying a change.",
            knowledge_level="pattern",
            pattern_key="validate-before-apply",
            evidence_status="decision",
        ),
    )

    def index_offline(note, embed):
        raise RuntimeError("neo4j unavailable")

    monkeypatch.setattr(service, "index_note", index_offline)

    response = service.remember_response(
        content="Validate before applying a change.",
        title="Reusable validation pattern",
        space_id="work",
    )

    assert response.status == "degraded"
    assert response.data["index_pending"] is True
    assert response.data["knowledge_level"] == "pattern"
    assert response.data["pattern_key"] == "validate-before-apply"
    assert service.get_note(response.data["note_id"]) is not None


def test_notes_by_date_filters_by_the_source_conversation_date(tmp_path: Path) -> None:
    """Temporal retrieval uses source dates rather than note ingestion time."""
    service = BrainService(make_settings(tmp_path / "brain"))
    service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Weekly deployment",
            space_id="work",
            source_refs=[
                SourceReference(
                    id="session-1",
                    locator="codex-session://2026/07/29/rollout.jsonl",
                    content_hash="hash-1",
                    occurred_on=date(2026, 7, 29),
                )
            ],
        ),
        "## Summary\nDeploy after validation.",
    )

    results = service.notes_by_date(date(2026, 7, 27), date(2026, 8, 2))

    assert [result.title for result in results] == ["Weekly deployment"]


def test_notes_by_date_is_stable_and_supports_offset_pagination(
    tmp_path: Path,
) -> None:
    """Temporal pages use one canonical order and expose a stable total."""
    service = BrainService(make_settings(tmp_path / "brain"))
    for index in range(3):
        service.vault.upsert_managed(
            NoteMetadata(
                type="task",
                title=f"Timeline item {index}",
                space_id="work",
                source_refs=[
                    SourceReference(
                        id=f"session-{index}",
                        locator=(
                            "codex-session://2026/07/"
                            f"{index + 1:02d}/rollout.jsonl"
                        ),
                        content_hash=f"hash-{index}",
                        occurred_on=date(2026, 7, index + 1),
                    )
                ],
            ),
            f"## Summary\nTimeline item {index}.",
        )

    first_page = service.notes_by_date(
        date(2026, 7, 1), date(2026, 7, 31), limit=2, offset=0
    )
    second_page = service.notes_by_date(
        date(2026, 7, 1), date(2026, 7, 31), limit=2, offset=2
    )
    coverage = service.date_coverage(date(2026, 7, 1), date(2026, 7, 31))

    assert [result.title for result in first_page] == [
        "Timeline item 0",
        "Timeline item 1",
    ]
    assert [result.title for result in second_page] == ["Timeline item 2"]
    assert coverage["notes_in_range"] == 3


def test_date_coverage_reports_missing_source_dates(tmp_path: Path) -> None:
    """Temporal diagnostics distinguish source dates from ingestion dates."""
    service = BrainService(make_settings(tmp_path / "brain"))
    service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Undated source",
            space_id="work",
            source_refs=[
                SourceReference(
                    id="session-undated",
                    locator="codex-session://unknown/rollout.jsonl",
                    content_hash="hash-undated",
                )
            ],
        ),
        "## Summary\nSource date is unavailable.",
    )

    coverage = service.date_coverage(date(2026, 7, 1), date(2026, 7, 31))

    assert coverage["notes_with_source_refs"] == 1
    assert coverage["source_refs_with_dates"] == 0
    assert coverage["notes_without_source_dates"] == 1


def test_reflection_requires_independent_positive_evidence(tmp_path: Path) -> None:
    """A validated pair of experiences creates one active workflow."""
    service = BrainService(make_settings(tmp_path / "brain"))
    first = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform first",
            space_id="work",
            evidence_status="confirmed_success",
            source_refs=[
                SourceReference(
                    id="session-a",
                    locator="codex-session://a",
                    content_hash="hash-a",
                    session_id="session-a",
                )
            ],
            claims=[
                Claim(
                    id="claim-terraform-a",
                    text="Run Terraform plan before apply.",
                    claim_key="terraform.plan-before-apply",
                    claim_type="tool_observation",
                    confidence=0.95,
                    evidence=[
                        EvidenceSpan(
                            source_id="session-a",
                            session_id="session-a",
                            event_start=1,
                            event_end=2,
                            fragment="Run Terraform plan before apply.",
                            precision="exact",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nRun Terraform plan before apply.",
    )
    second = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform second",
            space_id="work",
            evidence_status="decision",
            source_refs=[
                SourceReference(
                    id="session-b",
                    locator="codex-session://b",
                    content_hash="hash-b",
                    session_id="session-b",
                )
            ],
            claims=[
                Claim(
                    id="claim-terraform-b",
                    text="Use Terraform plan before apply.",
                    claim_key="terraform.plan-before-apply",
                    claim_type="user_decision",
                    confidence=0.95,
                    evidence=[
                        EvidenceSpan(
                            source_id="session-b",
                            session_id="session-b",
                            event_start=3,
                            event_end=4,
                            fragment="Use Terraform plan before apply.",
                            precision="exact",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nUse Terraform plan before apply.",
    )
    service.gateway.reflect = lambda experiences, workflows: ReflectionKnowledge(
        workflows=[
            WorkflowProposal(
                title="Terraform validation",
                summary="Validate before apply.",
                steps=[
                    {
                        "text": "Run plan",
                        "evidence_claim_ids": [
                            "claim-terraform-a",
                            "claim-terraform-b",
                        ],
                    }
                ],
                evidence_note_ids=[str(first.metadata.id), str(second.metadata.id)],
                labels=["terraform"],
                confidence=0.9,
            )
        ]
    )

    result = service.reflect()

    assert result["workflows"] == 1
    workflows = [
        note for note in service.vault.iter_notes() if note.metadata.type == "workflow"
    ]
    assert len(workflows) == 1
    assert workflows[0].metadata.labels == ["technology:terraform"]


def test_reflection_accepts_one_source_with_continuous_initial_confidence(
    tmp_path: Path,
) -> None:
    """A single source creates a confirm-first workflow capped at 0.65."""
    service = BrainService(make_settings(tmp_path / "brain"))
    evidence = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform run",
            space_id="work",
            evidence_status="confirmed_success",
            source_refs=[
                SourceReference(
                    id="source-a",
                    locator="codex-session://a",
                    content_hash="hash-a",
                    session_id="session-a",
                )
            ],
            claims=[
                Claim(
                    id="claim-a",
                    text="Run Terraform plan before apply.",
                    claim_key="terraform.plan-before-apply",
                    claim_type="tool_observation",
                    evidence=[
                        EvidenceSpan(
                            source_id="source-a",
                            session_id="session-a",
                            fragment="Run Terraform plan before apply.",
                            precision="source",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nRun Terraform plan before apply.",
    )
    service.gateway.reflect = lambda experiences, workflows: ReflectionKnowledge(
        workflows=[
            WorkflowProposal(
                title="Terraform validation",
                summary="Validate before apply.",
                steps=[
                    {"text": "Run plan", "evidence_claim_ids": ["claim-a"]}
                ],
                evidence_note_ids=[str(evidence.metadata.id)],
                confidence=0.95,
            )
        ]
    )

    assert service.reflect()["workflows"] == 1
    workflow = next(
        note for note in service.vault.iter_notes() if note.metadata.type == "workflow"
    )
    assert workflow.metadata.confidence == 0.65


def test_reflection_materializes_pattern_from_independent_adapters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Two scoped implementations create one agnostic derived pattern."""
    service = BrainService(make_settings(tmp_path / "brain"))
    for source_id, title, level in [
        ("adapter-a", "Codeen gateway adapter", "example"),
        ("adapter-b", "Cloud Run adapter", "adapter"),
    ]:
        service.vault.upsert_managed(
            NoteMetadata(
                type="task",
                title=title,
                space_id="work",
                knowledge_level=level,
                pattern_key="openai-compatible-llm-gateway-integration",
                source_refs=[
                    SourceReference(
                        id=source_id,
                        locator=f"task://{source_id}",
                        content_hash=f"hash-{source_id}",
                    )
                ],
            ),
            f"## Summary\n{title}.",
        )
    service.gateway.reflect = lambda experiences, workflows: ReflectionKnowledge()
    monkeypatch.setattr(service, "_sync_projection", lambda note: True)

    result = service.reflect()

    assert result["patterns"] == 1
    patterns = [
        note for note in service.vault.iter_notes() if note.metadata.type == "pattern"
    ]
    assert len(patterns) == 1
    assert patterns[0].metadata.knowledge_level == "pattern"
    assert patterns[0].metadata.pattern_key == (
        "openai-compatible-llm-gateway-integration"
    )
    assert patterns[0].metadata.evidence_status == "investigation"
    assert "not an execution record" in patterns[0].content


def test_reflection_accepts_repeated_assistant_suggestion_without_user_feedback(
    tmp_path: Path,
) -> None:
    """Repeated positive suggestions become workflow evidence automatically."""
    service = BrainService(make_settings(tmp_path / "brain"))
    notes = []
    for index, session_id in enumerate(("session-a", "session-b")):
        notes.append(
            service.vault.upsert_managed(
                NoteMetadata(
                    type="task",
                    title=f"Terraform formatting run {index}",
                    space_id="work",
                    evidence_status="confirmed_success",
                    source_refs=[
                        SourceReference(
                            id=session_id,
                            locator=f"codex-session://{session_id}",
                            content_hash=f"hash-{session_id}",
                            session_id=session_id,
                        )
                    ],
                    claims=[
                        Claim(
                            id=f"claim-format-{index}",
                            text="Run terraform fmt before validation.",
                            claim_key="terraform.run-fmt-before-validation",
                            claim_type="assistant_suggestion",
                            evidence=[
                                EvidenceSpan(
                                    source_id=session_id,
                                    session_id=session_id,
                                    fragment="terraform fmt completed successfully",
                                    precision="exact",
                                )
                            ],
                        )
                    ],
                ),
                "## Summary\nRun terraform fmt before validation.",
            )
        )

    proposal = WorkflowProposal(
        title="Terraform formatting validation",
        summary="Format Terraform before validation.",
        steps=[
            {
                "text": "Run terraform fmt.",
                "evidence_claim_ids": [
                    "claim-format-0",
                    "claim-format-1",
                ],
            }
        ],
        evidence_note_ids=[str(note.metadata.id) for note in notes],
        confidence=0.8,
    )

    assert _valid_workflow_evidence(notes, proposal)


def test_reflection_rejects_single_assistant_suggestion(
    tmp_path: Path,
) -> None:
    """One unconfirmed suggestion cannot become workflow evidence."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform formatting proposal",
            space_id="work",
            evidence_status="confirmed_success",
            source_refs=[
                SourceReference(
                    id="session-a",
                    locator="codex-session://a",
                    content_hash="hash-a",
                    session_id="session-a",
                )
            ],
            claims=[
                Claim(
                    id="claim-format",
                    text="Run terraform fmt before validation.",
                    claim_key="terraform.run-fmt-before-validation",
                    claim_type="assistant_suggestion",
                    evidence=[
                        EvidenceSpan(
                            source_id="session-a",
                            session_id="session-a",
                            fragment="terraform fmt completed successfully",
                            precision="exact",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nRun terraform fmt before validation.",
    )

    proposal = WorkflowProposal(
        title="Terraform formatting validation",
        summary="Format Terraform before validation.",
        steps=[
            {"text": "Run terraform fmt.", "evidence_claim_ids": ["claim-format"]}
        ],
        evidence_note_ids=[str(note.metadata.id)],
        confidence=0.8,
    )

    assert not _valid_workflow_evidence([note], proposal)


def test_reflection_accepts_single_successful_action_as_confirm_first_candidate(
    tmp_path: Path,
) -> None:
    """A concrete successful action can create a low-confidence candidate."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Pytest imports fixed",
            space_id="work",
            evidence_status="confirmed_success",
            source_refs=[
                SourceReference(
                    id="session-a",
                    locator="codex-session://a",
                    content_hash="hash-a",
                    session_id="session-a",
                )
            ],
            actions=[
                ActionSignature(
                    action_key="fix_pytest_imports",
                    outcome="pytest passed after the import fix",
                    tools=["pytest"],
                    route="add tests/conftest.py",
                )
            ],
            claims=[
                Claim(
                    id="claim-pytest",
                    text="Pytest passed after the import fix.",
                    claim_key="pytest.imports.fixed",
                    claim_type="assistant_suggestion",
                    evidence=[
                        EvidenceSpan(
                            source_id="session-a",
                            session_id="session-a",
                            fragment="pytest passed after the import fix",
                            precision="exact",
                        )
                    ],
                )
            ],
        ),
        "## Summary\nPytest passed after the import fix.",
    )

    proposal = WorkflowProposal(
        title="Fix pytest imports",
        summary="Repair imports and rerun pytest.",
        action=ActionSignature(
            action_key="fix_pytest_imports",
            outcome="pytest passed",
            tools=["pytest"],
            route="add tests/conftest.py",
        ),
        steps=[
            {
                "text": "Add tests/conftest.py and rerun pytest.",
                "evidence_claim_ids": ["claim-pytest"],
            }
        ],
        evidence_note_ids=[str(note.metadata.id)],
        confidence=0.8,
    )

    assert _valid_workflow_evidence([note], proposal)


def test_reflection_does_not_count_segments_as_independent_sessions(
    tmp_path: Path,
) -> None:
    """Legacy segment references from one rollout remain one session."""
    service = BrainService(make_settings(tmp_path / "brain"))
    notes = []
    for index in range(2):
        source_id = f"codex-session-legacy-segment-{index}"
        notes.append(
            service.vault.upsert_managed(
                NoteMetadata(
                    type="task",
                    title=f"Legacy segment {index}",
                    space_id="work",
                    evidence_status="confirmed_success",
                    source_refs=[
                        SourceReference(
                            id=source_id,
                            locator=(
                                "codex-session://2026/03/17/rollout.jsonl"
                                f"#segment-{index}"
                            ),
                            content_hash=f"hash-{index}",
                        )
                    ],
                    claims=[
                        Claim(
                            id=f"claim-legacy-{index}",
                            text="The command succeeded.",
                            claim_key="command.succeeded",
                            claim_type="assistant_suggestion",
                            evidence=[
                                EvidenceSpan(
                                    source_id=source_id,
                                    fragment="command succeeded",
                                    precision="source",
                                )
                            ],
                        )
                    ],
                ),
                "## Summary\nThe command succeeded.",
            )
        )

    proposal = WorkflowProposal(
        title="Run the successful command",
        summary="Repeat the successful command.",
        steps=[
            {
                "text": "Run the command.",
                "evidence_claim_ids": [
                    "claim-legacy-0",
                    "claim-legacy-1",
                ],
            }
        ],
        evidence_note_ids=[str(note.metadata.id) for note in notes],
        confidence=0.8,
    )

    assert not _valid_workflow_evidence(notes, proposal)


def test_workflow_feedback_updates_vault_and_quarantines_low_confidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Feedback applies the configured reward/penalty and persists metadata."""
    service = BrainService(make_settings(tmp_path / "brain"))
    workflow = service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Deploy safely",
            space_id="work",
            confidence=0.60,
            claims=[Claim(id="claim", text="Deploy", claim_key="deploy")],
        ),
        "## Summary\nDeploy safely.",
    )
    synced: list[str] = []
    monkeypatch.setattr(
        service,
        "_sync_projection",
        lambda note: synced.append(str(note.metadata.id)) or True,
    )

    approved = service.record_workflow_feedback(
        str(workflow.metadata.id),
        "approved",
        "User confirmed the workflow.",
    )

    assert approved.status == "ok"
    assert approved.data["confidence"] == 0.75
    assert approved.data["success_count"] == 1
    assert workflow.metadata.usage_count == 0
    assert synced == [str(workflow.metadata.id)]
    assert service.vault.get(str(workflow.metadata.id)).metadata.confidence == 0.75

    failed = service.record_workflow_feedback(str(workflow.metadata.id), "failed")

    assert failed.data["confidence"] == 0.55
    assert failed.data["recommendation_state"] == "active"

    workflow.metadata.confidence = 0.35
    service.vault.update_metadata(workflow)
    quarantined = service.record_workflow_feedback(
        str(workflow.metadata.id), "failed"
    )
    assert quarantined.data["confidence"] == 0.15
    assert quarantined.data["recommendation_state"] == "quarantined"


def test_reflection_keeps_workflow_separate_from_evidence(tmp_path: Path) -> None:
    """Workflow persistence must not overwrite an evidence note."""
    service = BrainService(make_settings(tmp_path / "brain"))
    first = service.vault.upsert_managed(
            NoteMetadata(
            type="task",
            title="First deployment",
            space_id="work",
            evidence_status="confirmed_success",
                source_refs=[
                SourceReference(
                    id="session-first",
                    locator="codex-session://first",
                    content_hash="hash-first",
                    )
                ],
                claims=[
                    Claim(
                        id="claim-deploy-first",
                        text="Run the migration before deployment.",
                        claim_key="deployment.migration-before-deploy",
                        claim_type="tool_observation",
                        confidence=0.95,
                        evidence=[
                            EvidenceSpan(
                                source_id="session-first",
                                event_start=1,
                                event_end=2,
                                fragment="Run the migration before deployment.",
                                precision="exact",
                            )
                        ],
                    )
                ],
            ),
        "## Summary\nRun the migration before deployment.",
    )
    second = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Second deployment",
            space_id="work",
            evidence_status="decision",
                source_refs=[
                SourceReference(
                    id="session-second",
                    locator="codex-session://second",
                    content_hash="hash-second",
                    )
                ],
                claims=[
                    Claim(
                        id="claim-deploy-second",
                        text="Run the migration before deployment.",
                        claim_key="deployment.migration-before-deploy",
                        claim_type="user_decision",
                        confidence=0.95,
                        evidence=[
                            EvidenceSpan(
                                source_id="session-second",
                                event_start=3,
                                event_end=4,
                                fragment="Run the migration before deployment.",
                                precision="exact",
                            )
                        ],
                    )
                ],
            ),
        "## Summary\nRun the migration before deployment.",
    )
    evidence_snapshot = {
        str(note.metadata.id): (note.metadata.type, note.content.strip(), note.path)
        for note in (first, second)
    }
    service.gateway.reflect = lambda experiences, workflows: ReflectionKnowledge(
        workflows=[
            WorkflowProposal(
                title="Deployment migration",
                summary="Run the migration before deployment.",
                    steps=[
                        {
                            "text": "Run the migration",
                            "evidence_claim_ids": [
                                "claim-deploy-first",
                                "claim-deploy-second",
                            ],
                        }
                    ],
                evidence_note_ids=[str(first.metadata.id), str(second.metadata.id)],
                confidence=0.9,
            )
        ]
    )

    result = service.reflect()

    assert result["workflows"] == 1
    for note_id, snapshot in evidence_snapshot.items():
        note = service.vault.get(note_id)
        assert note is not None
        assert (note.metadata.type, note.content.strip(), note.path) == snapshot
    workflows = [
        note for note in service.vault.iter_notes() if note.metadata.type == "workflow"
    ]
    assert len(workflows) == 1
    assert workflows[0].path.startswith("Vault/work/Workflows/")


def test_reflection_does_not_activate_unknown_evidence(tmp_path: Path) -> None:
    """Unknown or fallback evidence remains searchable but inactive."""
    service = BrainService(make_settings(tmp_path / "brain"))
    first = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Unknown first",
            space_id="work",
            source_refs=[SourceReference(id="a", locator="a", content_hash="a")],
        ),
        "## Summary\nA possible process.",
    )
    second = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Unknown second",
            space_id="work",
            source_refs=[SourceReference(id="b", locator="b", content_hash="b")],
        ),
        "## Summary\nAnother possible process.",
    )
    service.gateway.reflect = lambda experiences, workflows: ReflectionKnowledge(
        workflows=[
            WorkflowProposal(
                title="Unsafe process",
                summary="Do it.",
                evidence_note_ids=[str(first.metadata.id), str(second.metadata.id)],
                confidence=1.0,
            )
        ]
    )

    result = service.reflect()

    assert result["workflows"] == 0


def test_hybrid_fusion_penalizes_weak_evidence_and_prefers_overlap() -> None:
    """RRF fusion combines channels and applies recommendation state penalties."""
    lexical = [
        SearchResult(
            note_id="strong",
            title="Terraform validation",
            note_type="task",
            space_id="work",
            path="strong.md",
            score=10.0,
            excerpt="Terraform plan",
            labels=["technology:terraform"],
            confidence=0.95,
        ),
        SearchResult(
            note_id="weak",
            title="Terraform idea",
            note_type="task",
            space_id="work",
            path="weak.md",
            score=9.0,
            excerpt="Terraform idea",
            recommendation_state="penalized",
            confidence=0.3,
        ),
    ]
    semantic = [lexical[0], lexical[1]]

    results = _fuse_results(
        lexical,
        semantic,
        tokens={"terraform"},
        labels={"technology:terraform"},
    )

    assert results[0].note_id == "strong"
    assert results[1].score < results[0].score
    assert results[1].quality_penalty == 0.35


def test_service_pure_helpers_preserve_evidence_and_ranking_invariants(
    tmp_path: Path,
) -> None:
    """Formatting, matching, and evidence helpers remain deterministic."""
    service = BrainService(make_settings(tmp_path / "brain"))
    claim_a = Claim(
        id="claim-a",
        text="Run the plan.",
        claim_key="terraform.plan",
        claim_type="tool_observation",
        evidence=[
            EvidenceSpan(
                source_id="source-a",
                session_id="session-a",
                fragment="Run the plan.",
                precision="exact",
            )
        ],
    )
    claim_b = Claim(
        id="claim-b",
        text="Use the plan before apply.",
        claim_key="terraform.plan",
        claim_type="user_decision",
        evidence=[
            EvidenceSpan(
                source_id="source-b",
                session_id="session-b",
                fragment="Use the plan before apply.",
                precision="exact",
            )
        ],
    )
    first = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform plan",
            space_id="work",
            evidence_status="confirmed_success",
            labels=["technology:terraform"],
            manual_labels=["important"],
            source_refs=[
                SourceReference(
                    id="source-a",
                    locator="session://a",
                    content_hash="hash-a",
                    session_id="session-a",
                )
            ],
            claims=[claim_a],
        ),
        "## Summary\nRun the plan.",
    )
    second = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform apply",
            space_id="work",
            evidence_status="decision",
            source_refs=[
                SourceReference(
                    id="source-b",
                    locator="session://b",
                    content_hash="hash-b",
                    session_id="session-b",
                )
            ],
            claims=[claim_b],
        ),
        "## Summary\nUse the plan before apply.",
    )
    proposal = WorkflowProposal(
        title="Terraform plan",
        summary="Validate before apply.",
        triggers=["Terraform change"],
        steps=[{"text": "Run the plan", "evidence_claim_ids": ["claim-a"]}],
        validation=["Plan succeeds"],
        labels=["technology:terraform"],
        evidence_note_ids=[str(first.metadata.id), str(second.metadata.id)],
        confidence=0.9,
    )

    assert _tokens("The Terraform plan") == ["terraform", "plan"]
    assert _effective_labels(first) == ["important", "technology:terraform"]
    assert _embedding_document(first).startswith("Terraform plan")
    assert _excerpt("first\nTerraform plan", {"terraform"}) == "Terraform plan"
    assert _excerpt("first\nsecond", {"missing"}) == "first\nsecond"
    assert _label_overlap(first.metadata.labels, {"technology:terraform"}) == 1.0
    assert _label_overlap([], {"technology:terraform"}) == 0.0
    assert all(
        _quality_factor(state) > 0
        for state in ["active", "penalized", "quarantined"]
    )
    assert _quality_factor("unknown") == 0.55
    assert _normalize_workflow_text(" Terraform / Plan ") == "terraform plan"
    assert _evidence_claims([first, second]) == [claim_a, claim_b]
    assert [item.id for item in _unique_source_refs([first, second])] == [
        "source-a",
        "source-b",
    ]
    assert _valid_workflow_evidence([first, second], proposal)
    assert "## Triggers" in _render_workflow(proposal)
    assert _search_result(first, 0.8).labels == ["important", "technology:terraform"]
    assert _safe_error(RuntimeError("secret-looking detail")) == "RuntimeError"

    workflow = service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Terraform plan",
            space_id="work",
            labels=["technology:terraform"],
        ),
        "workflow",
    )
    matched, state = _workflow_match(proposal, [workflow])
    assert matched is not None
    assert state == "strong"
    related = _related_experiences([first], [first, second], context_limit=2)
    assert related[0].metadata.id == first.metadata.id


def test_action_signature_keeps_distinct_implementation_routes_separate(
    tmp_path: Path,
) -> None:
    """One action may have multiple workflows without being deduplicated."""
    service = BrainService(make_settings(tmp_path / "brain"))
    action = ActionSignature(
        action_key="iam.grant_access",
        subjects=["service_account"],
        objects=["dataset"],
        tools=["terraform"],
        route="terraform.skill",
        confidence=0.9,
    )
    proposal = WorkflowProposal(
        title="Grant dataset access with Terraform",
        summary="Apply the IAM binding with Terraform.",
        action=action,
        steps=[],
    )
    datahub_workflow = service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Grant dataset access with Terraform",
            space_id="work",
            actions=[
                action.model_copy(
                    update={"tools": ["datahub"], "route": "datahub.mcp"}
                )
            ],
        ),
        "workflow",
    )

    matched, state = _workflow_match(proposal, [datahub_workflow])

    assert matched is None
    assert state is None


def test_related_experiences_uses_or_relationship_signals(
    tmp_path: Path,
) -> None:
    """Historical context can match through claim keys or semantic scores."""
    service = BrainService(make_settings(tmp_path / "brain"))
    candidate = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Publish a release",
            space_id="work",
            claims=[
                Claim(
                    id="claim-candidate",
                    text="Publish the release.",
                    claim_key="release.publish",
                    claim_type="assistant_suggestion",
                )
            ],
        ),
        "## Summary\nPublish the release.",
    )
    claim_key_match = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Ship an artifact",
            space_id="work",
            claims=[
                Claim(
                    id="claim-key-match",
                    text="Ship the artifact.",
                    claim_key="release.publish",
                    claim_type="assistant_suggestion",
                )
            ],
        ),
        "## Summary\nShip the artifact.",
    )
    semantic_match = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Promote a package",
            space_id="work",
        ),
        "## Summary\nPromote the package.",
    )

    related = _related_experiences(
        [candidate],
        [candidate, claim_key_match, semantic_match],
        context_limit=3,
        semantic_scores={str(semantic_match.metadata.id): 0.85},
    )

    related_ids = {note.metadata.id for note in related}
    assert claim_key_match.metadata.id in related_ids
    assert semantic_match.metadata.id in related_ids


def test_reflection_cards_group_notes_by_action_key_and_keep_unkeyed_notes(
    tmp_path: Path,
) -> None:
    """Reflection receives explicit action-key groups for consolidation."""
    service = BrainService(make_settings(tmp_path / "brain"))
    terraform = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Terraform access",
            space_id="work",
            actions=[
                ActionSignature(
                    action_key="grant_dataset_access_terraform",
                    canonical_action_key="iam.grant_access",
                    tools=["terraform"],
                    route="terraform.skill",
                )
            ],
        ),
        "## Summary\nGrant access with Terraform.",
    )
    datahub = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Datahub access",
            space_id="work",
            actions=[
                ActionSignature(
                    action_key="grant_dataset_access_datahub",
                    canonical_action_key="iam.grant_access",
                    tools=["datahub"],
                    route="datahub.mcp",
                )
            ],
        ),
        "## Summary\nGrant access with Datahub.",
    )
    unkeyed = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Unkeyed note", space_id="work"),
        "## Summary\nAn unclassified experience.",
    )

    groups = _reflection_action_groups([terraform, datahub, unkeyed])
    assert [key for key, _ in groups] == [
        "(no-canonical-action-key)",
        "iam.grant_access",
    ]
    assert [note.metadata.title for note in groups[1][1]] == [
        "Terraform access",
        "Datahub access",
    ]

    cards = _reflection_cards([terraform, datahub, unkeyed])
    assert "CANONICAL_ACTION_GROUP: iam.grant_access" in cards
    assert "CANONICAL_ACTION_GROUP: (no-canonical-action-key)" in cards
    assert cards.index("Terraform access") < cards.index("Datahub access")


def test_reflection_semantic_retrieval_returns_vector_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Reflection can use the existing Neo4j vector projection as context."""
    service = BrainService(make_settings(tmp_path / "brain"))
    candidate = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Publish a release", space_id="work"),
        "## Summary\nPublish the release.",
    )
    historical = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Promote an artifact", space_id="work"),
        "## Summary\nPromote the artifact.",
    )

    class FakeStore:
        """Minimal vector-store double for the reflection retriever."""

        def search_vector(self, embedding, space_id, limit):
            assert embedding == [1.0, 0.0]
            assert space_id == "work"
            assert limit == 2
            return [
                SearchResult(
                    note_id=str(historical.metadata.id),
                    title=historical.metadata.title,
                    note_type="task",
                    space_id="work",
                    path=historical.path,
                    score=0.91,
                    excerpt=historical.content,
                )
            ]

        def close(self):
            return None

    monkeypatch.setattr(
        service.gateway,
        "embed_batch",
        lambda texts, timeout_seconds: [[1.0, 0.0] for _ in texts],
    )
    monkeypatch.setattr(service, "_graph_store", lambda: FakeStore())

    scores = service._semantic_reflection_scores(
        [candidate],
        [candidate, historical],
        context_limit=2,
    )

    assert scores == {str(historical.metadata.id): 0.91}
