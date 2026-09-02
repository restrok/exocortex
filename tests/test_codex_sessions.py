"""Tests for the portable local Codex session adapter."""

import json
from pathlib import Path

from exocortex.codex_sessions import CodexSessionAdapter


def test_adapter_extracts_messages_and_tool_outcomes(tmp_path: Path) -> None:
    """Rollout events retain tool commands and their exit-code evidence."""
    session = tmp_path / "rollout-test.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Plan the migration.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "id": "call-1",
                            "name": "shell",
                            "arguments": {"command": "pytest -q"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": {"exit_code": 0, "stdout": "2 passed"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"text": "Use a versioned schema."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    records = list(CodexSessionAdapter(tmp_path, "work").records())

    assert len(records) == 1
    assert "Plan the migration." in records[0].content
    assert "Use a versioned schema." in records[0].content
    assert "tool_call name=shell" in records[0].content
    assert "exit_code=0" in records[0].content
    assert records[0].occurred_on is None


def test_adapter_extracts_claude_messages_and_timestamp(tmp_path: Path) -> None:
    """Claude top-level message events become Claude-specific source records."""
    session = tmp_path / "claude-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-08-06T09:10:11.123Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "Review MCP setup."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-06T09:10:12.123Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "The endpoint is healthy."}
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    records = list(CodexSessionAdapter(tmp_path, "work").records())

    assert len(records) == 1
    assert records[0].session_id.startswith("claude-session-")
    assert records[0].locator.startswith("claude-session://")
    assert records[0].occurred_on is not None
    assert records[0].occurred_on.isoformat() == "2026-08-06"
    assert "Review MCP setup." in records[0].content


def test_adapter_ignores_claude_subagent_files(tmp_path: Path) -> None:
    """Nested Claude subagent transcripts are not independent sessions."""
    main_session = tmp_path / "main.jsonl"
    main_session.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "Main task."},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    subagent_session = tmp_path / "subagents" / "agent-a.jsonl"
    subagent_session.parent.mkdir()
    subagent_session.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "Subtask."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    adapter = CodexSessionAdapter(tmp_path, "work")

    assert adapter.session_paths() == [main_session]
    assert len(list(adapter.records())) == 1


def test_adapter_derives_the_session_date_from_the_rollout_path(tmp_path: Path) -> None:
    """A standard Codex sessions layout becomes queryable temporal metadata."""
    session = tmp_path / "2026" / "07" / "29" / "rollout-2026-07-29T10-00-00.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": "Review last week's work.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    record = next(CodexSessionAdapter(tmp_path, "work").records())

    assert record.occurred_on is not None
    assert record.occurred_on.isoformat() == "2026-07-29"


def test_adapter_uses_a_stable_path_identity_and_segments_clear_topic_shifts(
    tmp_path: Path,
) -> None:
    """Different topics become separate experiences without content IDs."""
    session = tmp_path / "2026" / "07" / "29" / "rollout-topic.jsonl"
    session.parent.mkdir(parents=True)
    events = []
    for role, content in [
        ("user", "Configure Terraform module"),
        ("assistant", "Run Terraform validation."),
        ("user", "Validate Terraform plan"),
        ("assistant", "The plan is clean."),
        ("user", "Debug Slack notification"),
        ("assistant", "Inspect the notification payload."),
    ]:
        events.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": content,
                    },
                }
            )
        )
    session.write_text("\n".join(events) + "\n", encoding="utf-8")

    records = list(CodexSessionAdapter(tmp_path, "work").records())

    assert len(records) == 2
    assert records[0].session_id == records[1].session_id
    assert records[0].source_id != records[1].source_id
    assert records[0].locator.endswith("#segment-000000-000003")


def test_adapter_bounds_large_segments_with_two_message_overlap(tmp_path: Path) -> None:
    """Large thematic sessions are split at stable message boundaries."""
    session = tmp_path / "2026" / "07" / "29" / "rollout-large.jsonl"
    session.parent.mkdir(parents=True)
    lines = []
    for index in range(14):
        lines.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": f"Terraform validation context {index}",
                    },
                }
            )
        )
    session.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = list(CodexSessionAdapter(tmp_path, "work").records())

    assert len(records) == 2
    assert records[1].event_start < records[0].event_end
    assert "event=" in records[0].content
