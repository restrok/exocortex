"""Tests for the local Antigravity transcript adapter."""

from __future__ import annotations

import json
from pathlib import Path

from exocortex.antigravity_sessions import AntigravitySessionAdapter


def test_antigravity_adapter_extracts_messages_and_tool_calls(tmp_path: Path) -> None:
    """Antigravity transcript steps retain user requests, tool calls, and outputs."""
    conv_dir = tmp_path / "conv-12345" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript.jsonl"

    events = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-09-02T19:54:14Z",
            "content": "<USER_REQUEST>\nDeploy Cloud Run service\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-09-02T19:54:15Z",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": "gcloud run deploy --image test"},
                }
            ],
            "content": "Deploying Cloud Run service.",
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "created_at": "2026-09-02T19:54:20Z",
            "content": "Service [service-test] deployed successfully.",
        },
    ]

    transcript_file.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    adapter = AntigravitySessionAdapter(tmp_path, space_id="work")
    paths = adapter.session_paths()
    assert len(paths) == 1
    assert paths[0] == transcript_file

    records = adapter.records_for_path(transcript_file)
    assert len(records) == 1
    record = records[0]
    assert record.space_id == "work"
    assert "conv-12345" in record.session_id
    assert "Deploy Cloud Run service" in record.content
    assert "gcloud run deploy" in record.content
    assert "deployed successfully" in record.content
    assert record.occurred_on is not None
    assert str(record.occurred_on) == "2026-09-02"


def test_antigravity_adapter_clean_user_request_metadata(tmp_path: Path) -> None:
    """Antigravity user input cleans out system tags and metadata blocks."""
    conv_dir = tmp_path / "conv-metadata" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript.jsonl"

    content = (
        "<USER_REQUEST>\nFix PostgreSQL connection leak\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\ntime: 2026-09-02\n</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>\nmodel changed\n</USER_SETTINGS_CHANGE>"
    )

    events = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-09-02T12:00:00Z",
            "content": content,
        }
    ]
    transcript_file.write_text(json.dumps(events[0]) + "\n", encoding="utf-8")

    adapter = AntigravitySessionAdapter(tmp_path, space_id="work")
    records = adapter.records_for_path(transcript_file)
    assert len(records) == 1
    assert "Fix PostgreSQL connection leak" in records[0].content
    assert "<ADDITIONAL_METADATA>" not in records[0].content
    assert "<USER_SETTINGS_CHANGE>" not in records[0].content


def test_antigravity_adapter_is_closed(tmp_path: Path) -> None:
    """Inactive transcripts are marked as closed."""
    conv_dir = tmp_path / "conv-closed" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript.jsonl"
    transcript_file.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "hello"}) + "\n",
        encoding="utf-8",
    )

    adapter = AntigravitySessionAdapter(
        tmp_path, space_id="work", closed_after_seconds=0
    )
    assert adapter.is_closed(transcript_file)


def test_compact_payload_compresses_large_diffs_and_logs() -> None:
    """The payload compactor keeps headers/footers and collapses huge middles."""
    from exocortex.antigravity_sessions import _compact_payload

    # 1. Short text stays unchanged
    assert _compact_payload("short message") == "short message"

    # 2. Huge diff
    diff_lines = ["diff --git a/foo.py b/foo.py", "index 123..456 100644"]
    for i in range(100):
        diff_lines.append(f"+    line_{i} = {i}")
    diff_text = "\n".join(diff_lines)
    compacted_diff = _compact_payload(diff_text)
    assert "diff --git" in compacted_diff
    assert "... [omitted " in compacted_diff
    assert "line_99 = 99" in compacted_diff

    # 3. Huge terminal logs
    log_text = (
        "ERROR: Build failed\n" + ("traceback line\n" * 200) + "FATAL: Exit code 1"
    )
    compacted_log = _compact_payload(log_text)
    assert "ERROR: Build failed" in compacted_log
    assert "... [omitted " in compacted_log
    assert "FATAL: Exit code 1" in compacted_log


def test_user_intent_shift_classification() -> None:
    """Classify user intent into short continuations vs substantive directives."""
    from exocortex.antigravity_sessions import _is_user_intent_shift

    # Continuations
    assert not _is_user_intent_shift("ok")
    assert not _is_user_intent_shift("si dale")
    assert not _is_user_intent_shift("perfecto!")
    assert not _is_user_intent_shift("procedé")
    assert not _is_user_intent_shift("dale vamos")
    assert not _is_user_intent_shift("yes, proceed")

    # Intent shifts / New directives
    assert _is_user_intent_shift("creá un nuevo servicio para monitoreo")
    assert _is_user_intent_shift("implementá la autenticación con OAuth2")
    assert _is_user_intent_shift("tenemos que cambiar la arquitectura a microservicios")
    assert _is_user_intent_shift("how does the database migration work?")


def test_atomic_tool_execution_never_split(tmp_path: Path) -> None:
    """A tool call and its output are never split across different segments."""
    from exocortex.antigravity_sessions import parse_antigravity_records

    lines = []
    # Turn 0: User request
    lines.append(
        json.dumps(
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "content": (
                    "<USER_REQUEST>Execute big maintenance workflow</USER_REQUEST>"
                ),
                "created_at": "2026-09-02T10:00:00Z",
            }
        )
    )

    # Turn 1 & 2: First tool execution
    lines.append(
        json.dumps(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "run_command", "args": {"cmd": "apt-get update"}}
                ],
                "created_at": "2026-09-02T10:00:01Z",
            }
        )
    )
    lines.append(
        json.dumps(
            {
                "step_index": 2,
                "type": "GENERIC",
                "content": "Hit:1 http://deb.debian.org/debian stable InRelease" * 100,
                "created_at": "2026-09-02T10:00:05Z",
            }
        )
    )

    # Turn 3: Assistant explanation
    lines.append(
        json.dumps(
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "content": "Package list updated successfully.",
                "created_at": "2026-09-02T10:00:06Z",
            }
        )
    )

    # Turn 4: Next user task (intent shift)
    lines.append(
        json.dumps(
            {
                "step_index": 4,
                "type": "USER_INPUT",
                "content": (
                    "Ahora implementá el nuevo endpoint de salud para el clúster"
                ),
                "created_at": "2026-09-02T10:05:00Z",
            }
        )
    )

    records = parse_antigravity_records(lines, conversation_id="atomic-test")
    assert len(records) >= 1
    # Check that tool call and its output appear together in the same record
    first_record = records[0]
    assert "tool_execution name=run_command" in first_record.content
    assert "apt-get update" in first_record.content
    assert "Hit:1" in first_record.content


def test_semantic_windowing_with_mock_gateway(tmp_path: Path) -> None:
    """Semantic drift triggers natural segment cuts."""
    from exocortex.antigravity_sessions import parse_antigravity_records

    class MockGateway:
        def embed_batch(
            self, texts: list[str], timeout_seconds: int = 10
        ) -> list[list[float]]:
            embeddings = []
            for t in texts:
                # Orthogonal vectors for topic A vs topic B
                if "kubernetes" in t.lower() or "cluster" in t.lower():
                    embeddings.append([1.0, 0.0, 0.0])
                else:
                    embeddings.append([0.0, 1.0, 0.0])
            return embeddings

    # Create lines with substantial text crossing MIN_WINDOW_CHARS
    bread_prompt = (
        "Ahora cambiemos de tema: hornear pan de masa madre y recetas de cocina " * 150
    )
    lines = [
        json.dumps(
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "content": "Deploy kubernetes cluster " * 200,
                "created_at": "2026-09-02T10:00:00Z",
            }
        ),
        json.dumps(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Kubernetes cluster configuration applied. " * 100,
                "created_at": "2026-09-02T10:01:00Z",
            }
        ),
        json.dumps(
            {
                "step_index": 2,
                "type": "USER_INPUT",
                "content": bread_prompt,
                "created_at": "2026-09-02T10:05:00Z",
            }
        ),
    ]

    records = parse_antigravity_records(
        lines,
        conversation_id="semantic-test",
        gateway=MockGateway(),
    )
    assert len(records) >= 2
    assert "kubernetes" in records[0].content.lower()
    assert "masa madre" in records[1].content.lower()
