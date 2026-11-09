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


def test_render_dashboard_html_escapes_xss(tmp_path: Path) -> None:
    """Verify </script> and <script in note fields cannot break out of script tags."""
    settings = make_settings(tmp_path / "brain")
    service = FakeService(settings)
    service.note.metadata.title = "Evil </script><script>alert(1)</script>"
    service.note.content = "Body with </script><SCRIPT>evil()</SCRIPT>"

    html = render_dashboard_html(service)

    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html
    assert "<\\u0073cript>" in html
    assert "<\\u0053CRIPT>" in html


def test_get_dashboard_data_filters_personal_notes_and_caches_doctor(
    tmp_path: Path,
) -> None:
    """Verify personal spaces are excluded from recent_notes and doctor is cached."""
    settings = make_settings(tmp_path / "brain")
    personal_note = VaultNote(
        metadata=NoteMetadata(
            type="task",
            title="Secret Personal Note",
            space_id="personal-fsirio",
            owner="fsirio",
            recommendation_state="active",
        ),
        content="Private content",
        path="Vault/personal-fsirio/Tasks/secret.md",
    )

    class MultiNoteService(FakeService):
        def __init__(self, s) -> None:
            super().__init__(s)
            self.doctor_calls = 0

        @property
        def vault(self):
            class _V:
                def iter_notes(_self):
                    return [self.note, personal_note]

            return _V()

        def doctor(self):
            self.doctor_calls += 1
            return super().doctor()

    multi_service = MultiNoteService(settings)
    data = get_dashboard_data(multi_service)
    data_second = get_dashboard_data(multi_service)

    assert data["vault"]["total_notes"] == 2
    assert [n["title"] for n in data["recent_notes"]] == ["SRE Pattern"]
    assert data_second["vault"]["total_notes"] == 2
    assert multi_service.doctor_calls == 1

    data_with_personal = get_dashboard_data(multi_service, include_personal=True)
    assert len(data_with_personal["recent_notes"]) == 2
