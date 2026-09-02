"""Tests for the command handlers at the CLI boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import frontmatter
import pytest

from exocortex import cli
from exocortex.models import NoteMetadata, ResponseEnvelope, SearchResult


class _FakeService:
    """Small service double covering command serialization paths."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = SimpleNamespace(
            default_space="work",
            review_dir=tmp_path / "Review",
        )
        self.settings.review_dir.mkdir()
        self.calls: list[tuple[str, object]] = []
        self.labels = SimpleNamespace(aliases=lambda: {"tf": "technology:terraform"})
        self.note = SimpleNamespace(
            metadata=NoteMetadata(type="task", title="Remembered", space_id="work"),
            path="Vault/work/Tasks/remembered.md",
        )

    def initialize(self) -> None:
        self.calls.append(("initialize", None))

    def doctor(self) -> SimpleNamespace:
        return SimpleNamespace(
            vault="ok",
            gateway="ok",
            neo4j="ok",
            detail={},
        )

    def _ingestor(self) -> SimpleNamespace:
        return SimpleNamespace(
            ingest_file=lambda source, space, extract: [
                SimpleNamespace(source_id=str(source), extracted=extract)
            ]
        )

    def ingest_codex(self, **kwargs: object) -> list[object]:
        self.calls.append(("ingest_codex", kwargs))
        return [SimpleNamespace(source_id="session-1")]

    def extraction_canary(self) -> dict[str, object]:
        return {"status": "passed", "validated_items": 1}

    def sync(self, **kwargs: object) -> int:
        self.calls.append(("sync", kwargs))
        return 2

    def search_response(
        self,
        query: str,
        **kwargs: object,
    ) -> ResponseEnvelope:
        return ResponseEnvelope(status="ok", method="search", data=[], meta=kwargs)

    def list_by_label(self, *args: object, **kwargs: object) -> list[SearchResult]:
        return [
            SearchResult(
                note_id="note-1",
                title="Terraform",
                note_type="task",
                space_id="work",
                path="note.md",
                score=1.0,
                excerpt="Terraform",
            )
        ]

    def reflect(self, **kwargs: object) -> dict[str, object]:
        return {"status": "reflected", "processed": 1, **kwargs}

    def learning_status(self) -> dict[str, int]:
        return {"processed_notes": 1, "pending_notes": 0, "active_workflows": 0}

    def notes_by_date(self, *args: object, **kwargs: object) -> list[SearchResult]:
        return []

    def date_coverage(self, *args: object, **kwargs: object) -> dict[str, int]:
        return {"notes_in_range": 0}

    def rebuild(self) -> int:
        return 3

    def repair_report(self) -> dict[str, object]:
        return {"status": "ok"}

    def repair_apply(self) -> dict[str, object]:
        return {"status": "applied"}

    def repair_rollback(self, backup: Path) -> dict[str, object]:
        return {"status": "rolled_back", "backup": str(backup)}

    def backfill(self, **kwargs: object) -> dict[str, object]:
        return {"status": "completed", **kwargs}

    def retry_fallbacks(self, **kwargs: object) -> dict[str, object]:
        return {"status": "completed", **kwargs}

    def audit(self) -> list[str]:
        return []

    def export(self, output_dir: Path | None = None) -> Path:
        result = (output_dir or Path(".")) / "export.tar.gz"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.touch()
        return result

    def remember_response(self, *args: object, **kwargs: object) -> ResponseEnvelope:
        return ResponseEnvelope(
            status="stored",
            method="remember",
            data={"note_id": "note-1", "note_path": "note.md"},
        )

    def promote(self, source_id: str, index: bool = True) -> SimpleNamespace:
        return self.note

    def reject(self, source_id: str) -> None:
        self.calls.append(("reject", source_id))


def test_command_handlers_emit_v2_responses(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Core handlers forward options and serialize implementation types."""
    service = _FakeService(tmp_path)
    monkeypatch.setattr(cli, "_service", lambda: service)
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")

    cli.init()
    cli.doctor()
    cli.ingest(source, space="work", extract=False)
    cli.ingest_codex(tmp_path, space="work", extract=False)
    cli.extraction_canary()
    cli.sync(embed=False)
    cli.search("terraform", space="work", limit=2)
    cli.list_by_label(["terraform"])
    cli.reflect(limit=2)
    cli.learning_status()
    cli.labels()
    cli.timeline("2026-08-01", "2026-08-02")
    cli.rebuild()
    cli.repair_report()
    cli.repair_apply()
    backup = tmp_path / "backup.tar.gz"
    backup.touch()
    cli.repair_rollback(backup)
    cli.backfill(all_sources=True, batch_size=3, resume=False)
    cli.retry_fallbacks(all_sources=True, batch_size=3)
    cli.audit()
    output_dir = tmp_path / "exports"
    cli.export_brain(output_dir)
    cli.remember("memory", title="Title", space="work")
    cli.review_reject("candidate-1")

    output = capsys.readouterr().out
    assert '"status": "ok"' in output
    assert ("sync", {"embed": False}) in service.calls
    assert ("reject", "candidate-1") in service.calls


def test_cli_timeline_rejects_invalid_dates(tmp_path: Path, monkeypatch) -> None:
    """Date parsing fails before a service call when input is malformed."""
    monkeypatch.setattr(cli, "_service", lambda: _FakeService(tmp_path))

    with pytest.raises(cli.typer.BadParameter):
        cli.timeline("not-a-date", "2026-08-02")


def test_cli_backfill_surfaces_degraded_status_and_exit_code(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Operational failures are not reported as successful CLI envelopes."""
    service = _FakeService(tmp_path)
    service.backfill = lambda **kwargs: {
        "status": "degraded",
        "processed": 0,
        "failed": [],
        "remaining": 648,
        "reason": "ReadTimeout",
    }
    monkeypatch.setattr(cli, "_service", lambda: service)

    with pytest.raises(cli.typer.Exit) as error:
        cli.backfill(batch_size=1, max_failures=1)

    assert error.value.exit_code == 1
    assert '"status": "degraded"' in capsys.readouterr().out


def test_cli_review_and_eval_generation_paths(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Review listing and golden-set generation expose stable envelope output."""
    service = _FakeService(tmp_path)
    review_path = service.settings.review_dir / "candidate-1.md"
    review_path.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "candidate",
                source_id="candidate-1",
                space_id="work",
                reason="needs review",
            )
        ),
        encoding="utf-8",
    )
    service.vault = SimpleNamespace(
        read_review=lambda source_id: frontmatter.load(review_path)
    )
    monkeypatch.setattr(cli, "_service", lambda: service)
    cli.review_list()
    cli.review_promote("candidate-1", index=False)

    evaluation_module = __import__("exocortex.evaluation", fromlist=["evaluate"])
    monkeypatch.setattr(
        evaluation_module,
        "generate_cases",
        lambda current_service: [{"case_id": "case-1"}],
    )
    output_path = tmp_path / "golden.jsonl"
    cli.evaluate_quality(data=output_path, generate=True)

    assert output_path.read_text(encoding="utf-8").strip() == '{"case_id": "case-1"}'
    assert "review-list" in capsys.readouterr().out


def test_cli_config_init_and_install_codex(tmp_path: Path, monkeypatch) -> None:
    """Configuration commands write only the intended local integration files."""
    output = tmp_path / ".env"
    cli.config_init(output=output)
    assert "BRAIN_LLM_API_KEY=" in output.read_text(encoding="utf-8")

    home = tmp_path / "home"
    skill_source = tmp_path / "skill"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text("skill", encoding="utf-8")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))
    cli.config_install_codex(skill_source=skill_source)

    config = home / ".codex" / "config.toml"
    assert config.exists()
    assert "brain_remember" in config.read_text(encoding="utf-8")


def test_cli_install_claude_is_idempotent_and_allows_memory_capture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Claude installation registers one MCP and one shared skill copy."""
    home = tmp_path / "home"
    skill_source = tmp_path / "skill"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text("shared skill", encoding="utf-8")
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home))

    registered = False
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal registered
        commands.append(command)
        if command[:3] == ["claude", "mcp", "get"]:
            return SimpleNamespace(
                returncode=0 if registered else 1,
                stdout="URL: http://127.0.0.1:8765/mcp" if registered else "",
                stderr="",
            )
        registered = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.config_install_claude(skill_source=skill_source)
    cli.config_install_claude(skill_source=skill_source)

    add_commands = [
        command for command in commands if command[:3] == ["claude", "mcp", "add"]
    ]
    assert len(add_commands) == 1
    assert (
        home / ".claude" / "skills" / "codex-work-brain" / "SKILL.md"
    ).read_text(encoding="utf-8") == "shared skill"
    settings = json.loads(
        (home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert "mcp__codex-brain__brain_remember" in settings["permissions"]["allow"]
