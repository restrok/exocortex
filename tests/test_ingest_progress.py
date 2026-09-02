"""Tests for resumable Codex rollout ingestion progress."""

import json
import os
import time
from pathlib import Path

from exocortex.ingest import IngestResult
from exocortex.models import ExtractedKnowledge
from exocortex.service import (
    BrainService,
    _checkpoint_matches,
    _checkpoint_record_matches,
    _sessions_root_id,
    _update_ingest_summary,
)
from tests.conftest import make_settings


def _write_session(path: Path, messages: list[tuple[str, str]]) -> None:
    """Write a small response-item rollout fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": content,
            },
        }
        for role, content in messages
    ]
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_fallback_checkpoint_is_pending_and_counters_are_disjoint() -> None:
    """Fallbacks remain retryable and are not counted as successful promotions."""
    record = {
        "content_hash": "hash",
        "filter_version": "trivial-v1",
        "status": "promoted_fallback",
    }
    session = {
        "status": "completed",
        "mtime_ns": 1,
        "size": 2,
        "records": {"source-1": record},
    }
    summary: dict[str, object] = {
        "records_processed": 0,
        "records_failed": 0,
        "already_indexed": 0,
        "extracted": 0,
        "promoted": 0,
        "fallback": 0,
    }

    assert not _checkpoint_record_matches(record, "hash")
    assert not _checkpoint_matches(session, {"mtime_ns": 1, "size": 2})
    _update_ingest_summary(summary, "promoted_fallback")
    assert summary["fallback"] == 1
    assert summary["extracted"] == 0
    assert summary["promoted"] == 0


def test_ingest_status_uses_checkpoint_when_sessions_are_not_mounted(
    tmp_path: Path,
) -> None:
    """Status does not report fallback sessions as completed without a scan."""
    settings = make_settings(tmp_path / "brain")
    settings.codex_sessions_dir = tmp_path / "missing-sessions"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "sessions": {
            "complete.jsonl": {
                "status": "completed",
                "mtime_ns": 1,
                "size": 1,
                "records": {
                    "source-1": {
                        "status": "promoted",
                    }
                },
            },
            "fallback.jsonl": {
                "status": "completed",
                "mtime_ns": 1,
                "size": 1,
                "records": {
                    "source-2": {
                        "status": "promoted_fallback",
                    }
                },
            },
        },
        "last_run": {
            "sessions_total": 2,
            "sessions_pending": 1,
        },
    }
    checkpoint["root_id"] = _sessions_root_id(settings.codex_sessions_dir)
    (settings.state_dir / "codex-ingest-checkpoint.json").write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )

    status = BrainService(settings).ingest_status()

    assert status["sessions_total"] == 2
    assert status["sessions_completed"] == 1
    assert status["sessions_pending"] == 1
    assert status["source_scan"] == "checkpoint"
    assert status["source_available"] is False
    assert status["session_state_counts"] == {
        "ingested": 1,
        "pending": 1,
        "failed": 0,
        "not_available": 0,
    }


def test_missing_sessions_source_is_not_available(tmp_path: Path) -> None:
    """A missing rollout mount is explicit and is never reported completed."""
    settings = make_settings(tmp_path / "brain")
    settings.codex_sessions_dir = tmp_path / "missing-sessions"
    service = BrainService(settings)

    results = service.ingest_codex(only_closed=True)
    status = service.ingest_status()

    assert results == []
    assert status["source_available"] is False
    assert status["session_state_counts"] == {
        "ingested": 0,
        "pending": 0,
        "failed": 0,
        "not_available": 1,
    }
    assert status["last_run"]["status"] == "not_available"


def test_ingest_checkpoints_are_isolated_by_sessions_root(tmp_path: Path) -> None:
    """A Claude-root checkpoint cannot replace the default Codex checkpoint."""
    settings = make_settings(tmp_path / "brain")
    service = BrainService(settings)
    codex_root = settings.codex_sessions_dir
    claude_root = tmp_path / "claude-sessions"
    codex_checkpoint = {
        "root_id": _sessions_root_id(codex_root),
        "sessions": {"codex.jsonl": {"status": "completed"}},
    }
    claude_checkpoint = {
        "root_id": _sessions_root_id(claude_root),
        "sessions": {"claude.jsonl": {"status": "completed"}},
    }

    service._persist_codex_ingest_progress(codex_checkpoint, {})
    service._persist_codex_ingest_progress(claude_checkpoint, {})

    default_path = settings.state_dir / "codex-ingest-checkpoint.json"
    claude_path = settings.state_dir / (
        f"codex-ingest-checkpoint-{_sessions_root_id(claude_root)}.json"
    )
    assert json.loads(default_path.read_text(encoding="utf-8"))["root_id"] == (
        _sessions_root_id(codex_root)
    )
    assert json.loads(claude_path.read_text(encoding="utf-8"))["root_id"] == (
        _sessions_root_id(claude_root)
    )


def test_codex_ingestion_skips_unchanged_sessions(tmp_path: Path) -> None:
    """A completed rollout is not sent through ingestion again."""
    sessions_root = tmp_path / "sessions"
    session = sessions_root / "2026" / "08" / "05" / "rollout.jsonl"
    _write_session(
        session,
        [
            ("user", "Review the Terraform deployment."),
            ("assistant", "The deployment uses the existing module."),
        ],
    )
    settings = make_settings(tmp_path / "brain")
    settings.codex_sessions_dir = sessions_root
    service = BrainService(settings)
    calls: list[str] = []

    def fake_ingest(record, extract=True):
        del extract
        calls.append(record.source_id)
        return IngestResult(record.source_id, "promoted", note_id=record.source_id)

    service.ingest = fake_ingest

    first = service.ingest_codex(sessions_root=sessions_root, extract=False)
    second = service.ingest_codex(sessions_root=sessions_root, extract=False)

    assert len(first) == 1
    assert second == []
    assert len(calls) == 1
    checkpoint = settings.state_dir / "codex-ingest-checkpoint.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["sessions"]["2026/08/05/rollout.jsonl"]["status"] == "completed"


def test_codex_ingestion_retries_a_session_after_record_failure(tmp_path: Path) -> None:
    """A failed session remains pending so the next cycle retries it."""
    sessions_root = tmp_path / "sessions"
    session = sessions_root / "rollout.jsonl"
    _write_session(
        session,
        [
            ("user", "Review the deployment."),
            ("assistant", "The deployment is ready."),
        ],
    )
    closed_timestamp = time.time() - 7200
    os.utime(session, (closed_timestamp, closed_timestamp))
    settings = make_settings(tmp_path / "brain")
    settings.codex_sessions_dir = sessions_root
    service = BrainService(settings)
    calls = 0

    def failing_ingest(record, extract=True):
        del record, extract
        nonlocal calls
        calls += 1
        raise RuntimeError("fixture failure")

    service.ingest = failing_ingest

    result = service.ingest_codex(sessions_root=sessions_root, extract=False)
    status = service.ingest_status()

    assert result[0].status == "failed"
    assert calls == 1
    assert status["sessions_pending"] == 1
    checkpoint = settings.state_dir / "codex-ingest-checkpoint.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["sessions"]["rollout.jsonl"]["lifecycle_state"] == "failed"
    assert payload["sessions"]["rollout.jsonl"]["status"] == "failed"
    assert payload["last_run"]["records_failed"] == 1


def test_codex_ingestion_limits_calls_and_resumes_by_segment(
    tmp_path: Path,
) -> None:
    """A call limit stops one cycle and skips completed segments next time."""
    sessions_root = tmp_path / "sessions"
    session = sessions_root / "2026" / "08" / "05" / "rollout.jsonl"
    _write_session(
        session,
        [
            ("user", "Configure Terraform module"),
            ("assistant", "Use the existing module."),
            ("user", "Validate Terraform plan"),
            ("assistant", "The plan is clean."),
            ("user", "Debug docker error"),
            ("assistant", "Inspect docker logs."),
        ],
    )
    settings = make_settings(tmp_path / "brain")
    settings.codex_sessions_dir = sessions_root
    service = BrainService(settings)

    class BatchGateway:
        """Gateway double that returns one extraction per request."""

        def __init__(self) -> None:
            self.calls = 0

        def extract_batch(
            self,
            sources: list[dict[str, str]],
        ) -> dict[str, ExtractedKnowledge]:
            self.calls += 1
            return {
                source["source_id"]: ExtractedKnowledge(
                    title=source["title"],
                    summary="Validated engineering work.",
                    confidence=0.9,
                    evidence_status="confirmed_success",
                )
                for source in sources
            }

    gateway = BatchGateway()
    service.gateway = gateway

    first = service.ingest_codex(
        sessions_root=sessions_root,
        max_llm_calls=1,
        max_seconds=600,
        batch_size=1,
    )
    first_status = service.ingest_status()
    second = service.ingest_codex(
        sessions_root=sessions_root,
        max_llm_calls=1,
        max_seconds=600,
        batch_size=1,
    )
    second_status = service.ingest_status()

    assert len(first) == 1
    assert len(second) == 1
    assert gateway.calls == 2
    assert first_status["last_run"]["stop_reason"] == "max_llm_calls_reached"
    assert second_status["sessions_pending"] == 0
    assert second_status["last_run"]["records_unchanged"] == 1
