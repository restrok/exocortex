"""Tests for the bounded nightly scheduler cycle."""

from datetime import UTC, datetime
from pathlib import Path

from exocortex.scheduler import (
    _acquire_lock,
    _mark_run,
    _release_lock,
    _should_run,
    run_cycle,
)
from tests.conftest import make_settings


def test_scheduler_run_helpers_are_idempotent(tmp_path: Path) -> None:
    """Run markers and locks prevent duplicate cycles."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    now = datetime(2026, 8, 4, 3, tzinfo=UTC)

    assert _should_run(state_dir, now, 3)
    _mark_run(state_dir, now.date())
    assert not _should_run(state_dir, now, 3)
    lock = state_dir / "cycle.lock"
    assert _acquire_lock(lock)
    assert not _acquire_lock(lock)
    _release_lock(lock)


def test_scheduler_waits_until_hour_and_tolerates_missing_release_lock(
    tmp_path: Path,
) -> None:
    """The scheduler neither runs early nor fails when cleanup is already done."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    before_hour = datetime(2026, 8, 4, 2, tzinfo=UTC)

    assert not _should_run(state_dir, before_hour, 3)
    _release_lock(state_dir / "missing.lock")


def test_run_cycle_persists_bounded_status(tmp_path: Path) -> None:
    """One cycle records repair, backfill, ingestion, and reflection results."""

    class FakeService:
        """Small scheduler dependency double."""

        def repair_report(self) -> dict[str, object]:
            return {
                "moves": [],
                "state_changes": [],
                "metadata_changes": [],
                "duplicates": [],
            }

        def repair_apply(self) -> dict[str, object]:
            raise AssertionError("No repair should be applied.")

        def backfill(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["batch_size"] == 25
            return {"status": "completed", "processed": 0}

        def ingest_codex(self, only_closed: bool) -> list[object]:
            assert only_closed
            return [object()]

        def retry_fallbacks(self, **kwargs: object) -> dict[str, object]:
            assert kwargs == {"batch_size": 25, "process_all": False}
            return {"status": "completed", "extracted": 0}

        def sync(self, embed: bool) -> int:
            assert embed
            return 2

        def reflect(self) -> dict[str, object]:
            return {"status": "no_changes"}

    settings = make_settings(tmp_path / "brain")
    status = run_cycle(FakeService(), settings)

    assert status["ingested"] == 1
    assert status["indexed"] == 2
    assert status["phases"]["fallback_retry"]["status"] == "completed"
    assert (settings.state_dir / "learning-status.json").exists()
