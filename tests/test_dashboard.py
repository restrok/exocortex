"""Unit tests for the Exocortex dashboard module."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from exocortex.dashboard import get_dashboard_data, render_dashboard_html
from exocortex.models import NoteMetadata, VaultNote
from tests.conftest import make_settings


class FakeService:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.note = VaultNote(
            metadata=NoteMetadata(
                type="pattern",
                title="SRE Pattern",
                space_id="work",
                recommendation_state="active",
                confidence=0.85,
                labels=["topic:sre", "technology:python"],
            ),
            content="Summary\nA test pattern for SRE observability.",
            path="Vault/work/Patterns/sre-pattern.md",
        )

    class FakeVault:
        def __init__(self, note) -> None:
            self._note = note

        def iter_notes(self):
            return [self._note]

    @property
    def vault(self):
        return self.FakeVault(self.note)

    def doctor(self):
        return SimpleNamespace(
            vault="ok",
            gateway="ok",
            neo4j="ok",
            detail={},
        )


def test_get_dashboard_data(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "brain")
    service = FakeService(settings)

    data = get_dashboard_data(service)

    assert data["status"] == "ok"
    assert data["vault"]["total_notes"] == 1
    assert data["vault"]["by_type"] == {"pattern": 1}
    assert data["vault"]["by_state"] == {"active": 1}
    assert len(data["recent_notes"]) == 1
    assert data["recent_notes"][0]["title"] == "SRE Pattern"
    assert data["components"]["vault"]["status"] == "ok"


def test_render_dashboard_html(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "brain")
    service = FakeService(settings)

    html = render_dashboard_html(service)

    assert "<!DOCTYPE html>" in html
    assert "EXOCORTEX // COGNITIVE LAB" in html
    assert "/api/dashboard" in html
    assert "SRE Pattern" in html
