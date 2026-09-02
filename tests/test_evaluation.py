"""Tests for portable golden-set metrics."""

import json
from pathlib import Path

from exocortex.evaluation import (
    GoldenCase,
    _mean,
    _ndcg,
    evaluate,
    generate_cases,
    load_cases,
)
from exocortex.models import NoteMetadata
from exocortex.service import BrainService
from tests.conftest import make_settings


def test_offline_evaluation_reports_retrieval_and_abstention(tmp_path: Path) -> None:
    """The offline runner evaluates deterministic Vault retrieval."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Terraform plan", space_id="work"),
        "## Summary\nRun the Terraform plan.",
    )
    cases = [
        GoldenCase(
            case_id="known",
            category="retrieval",
            query="Terraform plan",
            expected_note_ids=(str(note.metadata.id),),
            relevance={str(note.metadata.id): 3},
        ),
        GoldenCase(
            case_id="unknown",
            category="ood",
            query="Kubernetes ArgoCD Helm",
            should_abstain=True,
        ),
    ]

    result = evaluate(service, cases)

    assert result["cases"] == 2
    assert result["recall_at_5"] == 1.0
    assert result["abstention_accuracy"] == 1.0


def test_evaluation_resolves_superseded_frozen_note_ids(tmp_path: Path) -> None:
    """Frozen references remain valid after a note is consolidated."""
    service = BrainService(make_settings(tmp_path / "brain"))
    canonical = service.vault.upsert_managed(
        NoteMetadata(type="workflow", title="Canonical workflow", space_id="work"),
        "## Summary\nUse the canonical workflow.",
    )
    superseded = service.vault.upsert_managed(
        NoteMetadata(type="workflow", title="Old workflow", space_id="work"),
        "## Summary\nUse the old workflow.",
    )
    superseded.metadata.superseded_by = str(canonical.metadata.id)
    service.vault.update_metadata(superseded)

    result = evaluate(
        service,
        [
            GoldenCase(
                case_id="superseded",
                category="workflow",
                query="Canonical workflow",
                expected_note_ids=(str(superseded.metadata.id),),
                relevance={str(superseded.metadata.id): 3},
            )
        ],
    )

    assert result["recall_at_5"] == 1.0
    assert result["mrr_at_10"] == 1.0


def test_golden_loader_generator_and_metric_helpers(tmp_path: Path) -> None:
    """The frozen JSONL contract and deterministic metric helpers are portable."""
    service = BrainService(make_settings(tmp_path / "brain"))
    for index in range(50):
        service.vault.upsert_managed(
            NoteMetadata(
                type="task",
                title=f"Task {index:02d}",
                space_id="work",
            ),
            f"## Summary\nTask {index}.",
        )
    for index in range(10):
        service.vault.upsert_managed(
            NoteMetadata(
                type="workflow",
                title=f"Workflow {index:02d}",
                space_id="work",
                confidence=0.9,
            ),
            f"## Summary\nWorkflow {index}.",
        )

    generated = generate_cases(service)
    assert len(generated) == 60
    path = tmp_path / "golden.jsonl"
    path.write_text(
        "\n".join(json.dumps(item) for item in generated),
        encoding="utf-8",
    )
    loaded = load_cases(path)
    assert len(loaded) == 60
    assert _ndcg([3, 0, 0]) == 1.0
    assert _ndcg([]) == 0.0
    assert _ndcg([0, 0]) == 0.0
    assert _mean([1.0, 3.0]) == 2.0
    assert _mean([]) == 0.0
