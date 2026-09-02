"""Tests for reversible canonical-data maintenance."""

import json
from pathlib import Path

import frontmatter
import pytest

from exocortex.gateway import GatewayError
from exocortex.ingest import IngestResult
from exocortex.maintenance import (
    _desired_state,
    _duplicate_workflows,
    _parse_date,
    _read_checkpoint,
    backfill_sources,
)
from exocortex.models import Claim, NoteMetadata, SourceReference
from exocortex.service import BrainService
from tests.conftest import make_settings


def test_repair_moves_legacy_note_and_rollback_restores_snapshot(
    tmp_path: Path,
) -> None:
    """Repair preserves metadata while rollback restores the pre-repair path."""
    service = BrainService(make_settings(tmp_path / "brain"))
    note = service.vault.upsert_managed(
        NoteMetadata(type="task", title="Repair me", space_id="work"),
        "## Summary\nRepair me.",
    )
    old_path = service.settings.data_dir / note.path
    legacy_path = service.settings.vault_dir / "work" / "Commands" / old_path.name
    legacy_path.parent.mkdir(parents=True)
    old_path.replace(legacy_path)
    note.path = str(legacy_path.relative_to(service.settings.data_dir))

    report = service.repair_report()
    assert report["moves"]

    result = service.repair_apply()
    repaired = next(service.vault.iter_notes())
    assert result["moved"] == 1
    assert repaired.metadata.schema_version == 2
    assert repaired.metadata.recommendation_state == "penalized"

    second_result = service.repair_apply()
    assert second_result["backup"] != result["backup"]

    service.repair_rollback(Path(str(result["backup"])))

    restored = next(service.vault.iter_notes())
    assert restored.path.endswith(f"Commands/{old_path.name}")
    assert restored.metadata.schema_version == 1


def test_backfill_all_persists_a_checkpoint_after_each_batch(tmp_path: Path) -> None:
    """The all-sources mode drains bounded batches and remains resumable."""
    service = BrainService(make_settings(tmp_path / "brain"))
    service.settings.sanitized_dir.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        path = service.settings.sanitized_dir / f"source-{index}.md"
        path.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    f"Source content {index}",
                    source_id=f"source-{index}",
                    title=f"Source {index}",
                    space_id="work",
                )
            ),
            encoding="utf-8",
        )

    class HealthyGateway:
        """Gateway double for deterministic source re-extraction."""

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

    class RecordingIngestor:
        """Ingestor double recording the force-reextract contract."""

        def __init__(self) -> None:
            self.records: list[tuple[str, bool, bool, bool]] = []

        def ingest(self, record, **kwargs: object) -> None:
            self.records.append(
                (
                    record.source_id,
                    bool(kwargs["extract"]),
                    bool(kwargs["force_reextract"]),
                    bool(kwargs["allow_fallback"]),
                )
            )

    ingestor = RecordingIngestor()
    service.gateway = HealthyGateway()
    service._ingestor = lambda: ingestor

    result = backfill_sources(service, batch_size=2, process_all=True)

    assert result["status"] == "completed"
    assert result["processed"] == 5
    assert result["remaining"] == 0
    assert len(ingestor.records) == 5
    assert all(record[1:] == (True, True, False) for record in ingestor.records)
    checkpoint = service.settings.state_dir / "backfill-checkpoint.json"
    payload = _read_checkpoint(checkpoint)
    assert payload["remaining"] == 0


def test_retry_fallbacks_selects_only_fallback_sources(tmp_path: Path) -> None:
    """Selective repair never sends a valid extraction back to the gateway."""
    service = BrainService(make_settings(tmp_path / "brain"))
    fallback_id = "fallback-source"
    valid_id = "valid-source"
    for source_id in (fallback_id, valid_id):
        path = service.settings.sanitized_dir / f"{source_id}.md"
        path.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    f"Technical content for {source_id}.",
                    source_id=source_id,
                    title=source_id,
                    locator=f"task://{source_id}",
                    space_id="work",
                )
            ),
            encoding="utf-8",
        )
    service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Fallback",
            space_id="work",
            confidence=0.3,
            evidence_status="unknown",
            recommendation_state="penalized",
            prompt_version="fallback-v1",
            extraction_status="fallback",
            source_refs=[
                SourceReference(
                    id=fallback_id,
                    locator=f"task://{fallback_id}",
                    content_hash="fallback-hash",
                )
            ],
        ),
        "## Summary\nFallback.",
    )
    service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Valid",
            space_id="work",
            prompt_version="extraction-v2",
            extraction_status="extracted",
            source_refs=[
                SourceReference(
                    id=valid_id,
                    locator=f"task://{valid_id}",
                    content_hash="valid-hash",
                )
            ],
        ),
        "## Summary\nValid.",
    )

    class RecordingIngestor:
        def __init__(self) -> None:
            self.source_ids: list[str] = []

        def ingest_batch(self, records, **kwargs: object) -> list[IngestResult]:
            assert kwargs["force_reextract"] is True
            assert kwargs["allow_fallback"] is False
            self.source_ids.extend(record.source_id for record in records)
            return [
                IngestResult(record.source_id, "promoted", note_id=record.source_id)
                for record in records
            ]

    ingestor = RecordingIngestor()
    service.extraction_canary = lambda: {
        "status": "passed",
        "validated_items": 1,
    }
    service._ingestor = lambda: ingestor

    result = service.retry_fallbacks(process_all=True)

    assert result["status"] == "completed"
    assert result["identified"] == 1
    assert ingestor.source_ids == [fallback_id]


def test_retry_fallbacks_keeps_partial_batch_progress(tmp_path: Path) -> None:
    """A strict retry records valid items and leaves omitted items pending."""
    service = BrainService(make_settings(tmp_path / "brain"))
    source_ids = ["fallback-one", "fallback-two"]
    for source_id in source_ids:
        path = service.settings.sanitized_dir / f"{source_id}.md"
        path.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    f"Technical content for {source_id}.",
                    source_id=source_id,
                    title=source_id,
                    locator=f"task://{source_id}",
                    space_id="work",
                )
            ),
            encoding="utf-8",
        )
        service.vault.upsert_managed(
            NoteMetadata(
                type="task",
                title=source_id,
                space_id="work",
                confidence=0.3,
                evidence_status="unknown",
                recommendation_state="penalized",
                prompt_version="fallback-v1",
                extraction_status="fallback",
                source_refs=[
                    SourceReference(
                        id=source_id,
                        locator=f"task://{source_id}",
                        content_hash=f"{source_id}-hash",
                    )
                ],
            ),
            f"## Summary\n{source_id}.",
        )

    class PartialRetryIngestor:
        """Ingestor double returning one success and one omitted item."""

        def ingest_batch(self, records, **kwargs: object) -> list[IngestResult]:
            assert kwargs["allow_fallback"] is False
            return [
                IngestResult(records[0].source_id, "promoted"),
                IngestResult(
                    records[1].source_id,
                    "failed",
                    error="Batch extraction omitted source.",
                ),
            ]

    service.extraction_canary = lambda: {"status": "passed"}
    service._ingestor = lambda: PartialRetryIngestor()

    result = service.retry_fallbacks(process_all=True)

    assert result["status"] == "partial"
    assert result["extracted"] == 1
    assert result["failed"] == ["fallback-two"]
    assert result["remaining"] == 1


def test_backfill_skips_current_v4_sources_without_gateway_calls(
    tmp_path: Path,
) -> None:
    """Already-current sources do not pay for a redundant extraction."""
    service = BrainService(make_settings(tmp_path / "brain"))
    source_id = "source-current"
    content_hash = "hash-current"
    service.vault.upsert_managed(
        NoteMetadata(
            schema_version=2,
            type="task",
            title="Current source",
            space_id="work",
            prompt_version="extraction-v4",
            source_refs=[
                SourceReference(
                    id=source_id,
                    locator="task://current",
                    content_hash=content_hash,
                )
            ],
        ),
        "## Summary\nAlready extracted.",
    )
    source = service.settings.sanitized_dir / f"{source_id}.md"
    source.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "Already extracted.",
                source_id=source_id,
                content_hash=content_hash,
                locator="task://current",
                space_id="work",
            )
        ),
        encoding="utf-8",
    )

    class HealthyGateway:
        """Gateway double that should only be queried for health."""

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

    class FailingIngestor:
        """Ingestor double proving redundant extraction was avoided."""

        def ingest(self, record, **kwargs: object) -> None:
            raise AssertionError("Current source should be skipped.")

    service.gateway = HealthyGateway()
    service._ingestor = lambda: FailingIngestor()

    result = backfill_sources(service, batch_size=1)

    assert result["status"] == "completed"
    assert result["processed"] == 0
    assert result["skipped"] == 1
    assert result["remaining"] == 0


def test_backfill_reprocesses_current_notes_without_usable_claim_evidence(
    tmp_path: Path,
) -> None:
    """Current metadata does not hide notes whose claims need evidence repair."""
    service = BrainService(make_settings(tmp_path / "brain"))
    source_id = "source-missing-evidence"
    content_hash = "hash-missing-evidence"
    service.vault.upsert_managed(
        NoteMetadata(
            schema_version=2,
            type="decision",
            title="Decision without evidence",
            space_id="work",
            prompt_version="extraction-v2",
            claims=[
                Claim(
                    id="claim-1",
                    text="Use the validated approach.",
                    claim_key="validated.approach",
                    claim_type="assistant_suggestion",
                )
            ],
            source_refs=[
                SourceReference(
                    id=source_id,
                    locator="task://missing-evidence",
                    content_hash=content_hash,
                )
            ],
        ),
        "## Summary\nDecision without evidence.",
    )
    source = service.settings.sanitized_dir / f"{source_id}.md"
    source.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "Decision without evidence.",
                source_id=source_id,
                content_hash=content_hash,
                locator="task://missing-evidence",
                space_id="work",
            )
        ),
        encoding="utf-8",
    )

    class HealthyGateway:
        def health(self) -> dict[str, str]:
            return {"status": "ok"}

    class RecordingIngestor:
        def __init__(self) -> None:
            self.source_ids: list[str] = []

        def ingest(self, record, **kwargs: object) -> None:
            self.source_ids.append(record.source_id)
            assert kwargs["force_reextract"] is True
            assert kwargs["allow_fallback"] is False

    ingestor = RecordingIngestor()
    service.gateway = HealthyGateway()
    service._ingestor = lambda: ingestor

    result = backfill_sources(service)

    assert result["processed"] == 1
    assert result["skipped"] == 0
    assert ingestor.source_ids == [source_id]


def test_backfill_degrades_without_gateway_and_retries_failed_sources(
    tmp_path: Path,
) -> None:
    """Unavailable infrastructure never marks a source as completed."""
    service = BrainService(make_settings(tmp_path / "brain"))
    service.settings.sanitized_dir.mkdir(parents=True, exist_ok=True)
    source = service.settings.sanitized_dir / "source.md"
    source.write_text(frontmatter.dumps(frontmatter.Post("content")), encoding="utf-8")

    class UnhealthyGateway:
        def health(self) -> None:
            raise GatewayError("gateway unavailable")

    service.gateway = UnhealthyGateway()
    degraded = backfill_sources(service)
    assert degraded["status"] == "degraded"
    assert degraded["remaining"] == 1

    class HealthyGateway:
        def health(self) -> dict[str, str]:
            return {"status": "ok"}

    class FailingIngestor:
        def ingest(self, record, **kwargs: object) -> None:
            raise GatewayError("temporary failure")

    service.gateway = HealthyGateway()
    service._ingestor = lambda: FailingIngestor()
    partial = backfill_sources(service, batch_size=1)
    assert partial["status"] == "partial"
    assert partial["failed"] == [str(source)]


def test_backfill_stops_at_failure_limit_and_logs_batch_progress(
    tmp_path: Path,
    caplog,
) -> None:
    """A failing gateway cannot consume hours when the circuit breaker is set."""
    service = BrainService(make_settings(tmp_path / "brain"))
    for index in range(3):
        path = service.settings.sanitized_dir / f"failed-{index}.md"
        path.write_text(
            frontmatter.dumps(frontmatter.Post(f"content {index}")),
            encoding="utf-8",
        )

    class HealthyGateway:
        def health(self) -> dict[str, str]:
            return {"status": "ok"}

    class FailingIngestor:
        def ingest(self, record, **kwargs: object) -> None:
            raise GatewayError("invalid extraction")

    service.gateway = HealthyGateway()
    service._ingestor = lambda: FailingIngestor()

    with caplog.at_level("INFO"):
        result = backfill_sources(
            service,
            batch_size=2,
            process_all=True,
            max_failures=1,
        )

    assert result["stopped"] is True
    assert result["stop_reason"] == "max_failures_reached"
    assert result["remaining"] == 3
    assert "Backfill batch started" in caplog.text
    assert "Backfill failure limit reached" in caplog.text


def test_backfill_resume_preserves_pending_failures(tmp_path: Path) -> None:
    """A successful retry does not erase other pending checkpoint failures."""
    service = BrainService(make_settings(tmp_path / "brain"))
    service.settings.sanitized_dir.mkdir(parents=True, exist_ok=True)
    first = service.settings.sanitized_dir / "source-1.md"
    second = service.settings.sanitized_dir / "source-2.md"
    for path in (first, second):
        path.write_text(
            frontmatter.dumps(frontmatter.Post("content")),
            encoding="utf-8",
        )

    checkpoint = service.settings.state_dir / "backfill-checkpoint.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "completed": [],
                "failed": [str(second)],
                "remaining": 2,
            }
        ),
        encoding="utf-8",
    )

    class HealthyGateway:
        """Gateway double for a successful retry."""

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

    class SuccessfulIngestor:
        """Ingestor double accepting the pending source."""

        def ingest(self, record, **kwargs: object) -> None:
            del record, kwargs

    service.gateway = HealthyGateway()
    service._ingestor = lambda: SuccessfulIngestor()

    result = backfill_sources(service, batch_size=1)

    assert result["processed"] == 1
    assert result["failed"] == [str(second)]
    assert result["remaining"] == 1


def test_maintenance_helpers_cover_safe_states_and_duplicate_policy(
    tmp_path: Path,
) -> None:
    """Repair helpers preserve quarantine and classify conservative duplicates."""
    service = BrainService(make_settings(tmp_path / "brain"))
    notes = [
        service.vault.upsert_managed(
            NoteMetadata(type="workflow", title="Deploy safely", space_id="work"),
            "summary",
        ),
        service.vault.upsert_managed(
            NoteMetadata(type="workflow", title="Deploy safely", space_id="work"),
            "summary",
        ),
    ]
    notes[1].metadata.created_at = notes[0].metadata.created_at
    exact_duplicates = _duplicate_workflows(notes)

    assert exact_duplicates[0]["action"] == "supersede"
    assert _desired_state(notes[0]) == "penalized"
    notes[0].metadata.recommendation_state = "quarantined"
    assert _desired_state(notes[0]) == "quarantined"
    notes[0].metadata.superseded_by = str(notes[1].metadata.id)
    assert _desired_state(notes[0]) == "quarantined"
    assert _parse_date("2026-08-04") is not None
    assert _parse_date("not-a-date") is None
    assert _parse_date(None) is None

    invalid = service.settings.state_dir / "invalid.json"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text("not-json", encoding="utf-8")
    assert _read_checkpoint(invalid) == {"completed": []}
    with pytest.raises(ValueError):
        backfill_sources(service, batch_size=0)


def test_repair_consolidates_exact_duplicates_without_deleting_provenance(
    tmp_path: Path,
) -> None:
    """Exact duplicates merge evidence and leave the duplicate recoverable."""
    service = BrainService(make_settings(tmp_path / "brain"))
    first = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Shared operational note",
            space_id="work",
            source_refs=[
                SourceReference(
                    id="source-a",
                    locator="memory://a",
                    content_hash="hash-a",
                )
            ],
        ),
        "## Summary\nThe same validated operation.",
    )
    second = service.vault.upsert_managed(
        NoteMetadata(
            type="task",
            title="Shared operational note",
            space_id="work",
            source_refs=[
                SourceReference(
                    id="source-b",
                    locator="memory://b",
                    content_hash="hash-b",
                )
            ],
        ),
        "## Summary\nThe same validated operation.",
    )

    report = service.repair_report()
    result = service.repair_apply()
    notes = list(service.vault.iter_notes())
    canonical = next(note for note in notes if not note.metadata.superseded_by)
    duplicate = next(note for note in notes if note.metadata.superseded_by)

    assert any(item["action"] == "supersede" for item in report["duplicates"])
    assert result["duplicates"] == 1
    assert {ref.id for ref in canonical.metadata.source_refs} == {
        "source-a",
        "source-b",
    }
    assert duplicate.metadata.superseded_by == str(canonical.metadata.id)
    assert service.vault.get(str(first.metadata.id)) is not None
    assert service.vault.get(str(second.metadata.id)) is not None


def test_repair_modes_daily_and_full_performance(tmp_path: Path) -> None:
    """Daily mode detects exact duplicates in O(N); full mode runs fuzzy matching."""
    service = BrainService(make_settings(tmp_path / "brain"))

    service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Terraform Cloud Run Deployment Guide",
            space_id="work",
            labels=["topic:terraform"],
        ),
        "content 1",
    )
    service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Terraform Cloud Run Deployment Guide",
            space_id="work",
            labels=["topic:terraform"],
        ),
        "content 1",
    )
    service.vault.upsert_managed(
        NoteMetadata(
            type="workflow",
            title="Terraform Cloud Run Deployment Guide v2",
            space_id="work",
            labels=["topic:terraform"],
        ),
        "content 3",
    )

    daily_report = service.repair_report(mode="daily")
    assert daily_report["repair_mode"] == "daily"
    assert len(daily_report["duplicates"]) == 1
    assert daily_report["duplicates"][0]["action"] == "supersede"

    full_report = service.repair_report(mode="full")
    assert full_report["repair_mode"] == "full"
    assert len(full_report["duplicates"]) >= 2
