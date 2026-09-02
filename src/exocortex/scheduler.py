"""Nightly Codex ingestion, reflection, and graph synchronization."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from exocortex.config import Settings
from exocortex.service import BrainService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_LOGGER = logging.getLogger(__name__)


def run_cycle(service: BrainService, settings: Settings) -> dict[str, object]:
    """Run one idempotent learning cycle."""
    status: dict[str, object] = {
        "status": "running",
        "phase": "repair",
        "phases": {},
        "started_at": datetime.now(UTC).isoformat(),
    }
    _save_run_status(settings, status)
    try:
        _start_phase(settings, status, "repair")
        _LOGGER.info("Learning phase started phase=repair")
        repair = service.repair_report()
        repair_result = (
            service.repair_apply()
            if (
                repair["moves"]
                or repair["state_changes"]
                or repair["metadata_changes"]
                or repair["duplicates"]
            )
            else {"status": "not_needed"}
        )
        status["repair"] = repair_result
        _finish_phase(settings, status, "repair", repair_result)
        _LOGGER.info("Learning phase finished phase=repair")

        _start_phase(settings, status, "backfill")
        if settings.scheduler_backfill_enabled:
            _LOGGER.info("Learning phase started phase=backfill")
            backfill = service.backfill(batch_size=25, resume=True, process_all=False)
        else:
            backfill = {
                "status": "skipped",
                "reason": "disabled_in_daily_cycle",
            }
            _LOGGER.info(
                "Learning phase skipped phase=backfill reason=disabled_in_daily_cycle"
            )
        status["backfill"] = backfill
        _finish_phase(settings, status, "backfill", backfill)
        _LOGGER.info(
            "Learning phase finished phase=backfill status=%s processed=%s "
            "skipped=%s remaining=%s",
            backfill.get("status"),
            backfill.get("processed"),
            backfill.get("skipped"),
            backfill.get("remaining"),
        )

        _start_phase(settings, status, "fallback_retry")
        if settings.scheduler_fallback_retry_enabled:
            fallback_retry = service.retry_fallbacks(
                batch_size=25,
                process_all=False,
            )
        else:
            fallback_retry = {
                "status": "skipped",
                "reason": "disabled_in_daily_cycle",
            }
        status["fallback_retry"] = fallback_retry
        _finish_phase(settings, status, "fallback_retry", fallback_retry)

        _start_phase(settings, status, "ingest")
        _LOGGER.info("Learning phase started phase=ingest")
        results = service.ingest_codex(only_closed=True)
        ingest_status = getattr(service, "ingest_status", None)
        ingest_progress = (
            ingest_status()
            if callable(ingest_status)
            else {"records_processed": len(results)}
        )
        status["ingested"] = len(results)
        status["ingest"] = ingest_progress
        ingest_phase = dict(ingest_progress)
        last_run = ingest_progress.get("last_run")
        if isinstance(last_run, dict):
            ingest_phase["status"] = last_run.get("status", "completed")
            ingest_phase["reason"] = last_run.get("stop_reason")
        else:
            ingest_phase["status"] = "completed"
        _finish_phase(settings, status, "ingest", ingest_phase)
        _LOGGER.info(
            "Learning phase finished phase=ingest processed=%s pending_sessions=%s "
            "failed=%s",
            ingest_progress.get("records_processed"),
            ingest_progress.get("sessions_pending"),
            ingest_progress.get("records_failed"),
        )

        _start_phase(settings, status, "sync")
        _LOGGER.info("Learning phase started phase=sync")
        indexed = service.sync(embed=True)
        status["indexed"] = indexed
        _finish_phase(
            settings,
            status,
            "sync",
            {"status": "completed", "indexed": indexed},
        )
        _LOGGER.info("Learning phase finished phase=sync indexed=%d", indexed)

        _start_phase(settings, status, "reflect")
        _LOGGER.info("Learning phase started phase=reflect")
        reflection = service.reflect()
        status["reflection"] = reflection
        _finish_phase(settings, status, "reflect", reflection)
        _LOGGER.info(
            "Learning phase finished phase=reflect processed=%s workflows=%s",
            reflection.get("processed"),
            reflection.get("workflows"),
        )
    except Exception as error:  # pylint: disable=broad-except
        _fail_phase(settings, status, str(status["phase"]), error)
        status["status"] = "failed"
        status["error"] = error.__class__.__name__
        status["failed_at"] = datetime.now(UTC).isoformat()
        _save_run_status(settings, status)
        _LOGGER.error(
            "Learning cycle failed phase=%s error=%s",
            status["phase"],
            error.__class__.__name__,
        )
        raise

    last_ingest = ingest_progress.get("last_run", {})
    ingest_degraded = isinstance(last_ingest, dict) and last_ingest.get("status") in {
        "degraded",
        "partial",
    }
    status["status"] = (
        "degraded"
        if backfill.get("status") not in {"completed", "skipped"}
        or fallback_retry.get("status") not in {"completed", "skipped"}
        or ingest_degraded
        else "completed"
    )
    status["phase"] = "completed"
    status["completed_at"] = datetime.now(UTC).isoformat()
    _save_run_status(settings, status)
    _LOGGER.info("Completed learning cycle status=%s", status["status"])
    return status


def main() -> None:
    """Run the local cycle at the configured hour with missed-run recovery."""
    settings = Settings()
    service = BrainService(settings)
    while True:
        now = datetime.now(ZoneInfo(settings.timezone))
        if _should_run(settings.state_dir, now, settings.reflection_hour):
            lock_path = settings.state_dir / "learning-cycle.lock"
            if _acquire_lock(lock_path):
                try:
                    _LOGGER.info("Starting Codex Brain learning cycle.")
                    run_cycle(service, settings)
                    _mark_run(settings.state_dir, now.date())
                except Exception as error:  # pylint: disable=broad-except
                    _LOGGER.warning(
                        "Learning cycle failed: %s", error.__class__.__name__
                    )
                finally:
                    _release_lock(lock_path)
        time.sleep(60)


def _should_run(state_dir: Path, now: datetime, hour: int) -> bool:
    """Run after the configured hour once per local calendar day."""
    if now.hour < hour:
        return False
    path = state_dir / "last-learning-date"
    try:
        return path.read_text(encoding="utf-8").strip() != now.date().isoformat()
    except OSError:
        return True


def _mark_run(state_dir: Path, run_date: date) -> None:
    """Record the local date only after a successful complete cycle."""
    (state_dir / "last-learning-date").write_text(
        run_date.isoformat() + "\n", encoding="utf-8"
    )


def _save_run_status(settings: Settings, status: dict[str, object]) -> None:
    """Persist a bounded non-sensitive status record for diagnostics."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    path = settings.state_dir / "learning-status.json"
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def _start_phase(
    settings: Settings,
    status: dict[str, object],
    phase: str,
) -> None:
    """Persist the start of one scheduler phase."""
    status["phase"] = phase
    phases = status.setdefault("phases", {})
    if isinstance(phases, dict):
        phases[phase] = {
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        }
    _save_run_status(settings, status)


def _finish_phase(
    settings: Settings,
    status: dict[str, object],
    phase: str,
    result: dict[str, object],
) -> None:
    """Persist a bounded phase outcome and explicit reason."""
    phases = status.setdefault("phases", {})
    phase_status = str(result.get("status") or "completed")
    phase_result = {
        "status": phase_status,
        "reason": result.get("reason") or result.get("stop_reason"),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    if isinstance(phases, dict):
        started = phases.get(phase, {})
        if isinstance(started, dict):
            phase_result["started_at"] = started.get("started_at")
        phases[phase] = phase_result
    _save_run_status(settings, status)


def _fail_phase(
    settings: Settings,
    status: dict[str, object],
    phase: str,
    error: Exception,
) -> None:
    """Persist a non-sensitive failure reason for the active phase."""
    phases = status.setdefault("phases", {})
    if isinstance(phases, dict):
        started = phases.get(phase, {})
        started_at = started.get("started_at") if isinstance(started, dict) else None
        phases[phase] = {
            "status": "failed",
            "reason": error.__class__.__name__,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    _save_run_status(settings, status)


def _acquire_lock(path: Path) -> bool:
    """Acquire a process lock without a third-party dependency."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
    except FileExistsError:
        return False
    return True


def _release_lock(path: Path) -> None:
    """Release a lock created by this process."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
