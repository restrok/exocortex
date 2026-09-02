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

    adapter = AntigravitySessionAdapter(tmp_path, space_id="work", 
        closed_after_seconds=0)
    assert adapter.is_closed(transcript_file)
