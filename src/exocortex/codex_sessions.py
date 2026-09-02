"""Read-only adapter for local Codex rollout files and experiences."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from exocortex.ingest import SourceRecord, source_date_from_locator

_STOP_WORDS = {
    "about",
    "after",
    "also",
    "como",
    "con",
    "from",
    "have",
    "para",
    "that",
    "the",
    "this",
    "una",
    "with",
}
_MAX_WINDOW_MESSAGES = 12
_MAX_WINDOW_CHARS = 12_000
_WINDOW_OVERLAP = 2


@dataclass(frozen=True)
class SessionMessage:
    """One user or assistant message extracted from a rollout."""

    event_index: int
    role: str
    text: str
    occurred_on: date | None = None


class CodexSessionAdapter:
    """Convert local Codex rollout JSON Lines files into source records."""

    def __init__(
        self,
        sessions_root: Path,
        space_id: str,
        closed_after_seconds: int = 1800,
    ) -> None:
        """Configure an explicit read-only sessions directory."""
        self._sessions_root = sessions_root
        self._space_id = space_id
        self._closed_after_seconds = closed_after_seconds

    def records(self, only_closed: bool = False) -> Iterable[SourceRecord]:
        """Yield one source record per stable thematic experience."""
        for path in self.session_paths(only_closed=only_closed):
            yield from self.records_for_path(path)

    def session_paths(self, only_closed: bool = False) -> list[Path]:
        """Return eligible rollout paths in deterministic order."""
        if not self._sessions_root.exists():
            return []
        paths = sorted(
            path
            for path in self._sessions_root.rglob("*.jsonl")
            if "subagents" not in path.relative_to(self._sessions_root).parts
        )
        if only_closed:
            paths = [path for path in paths if self.is_closed(path)]
        return paths

    def records_for_path(self, path: Path) -> list[SourceRecord]:
        """Convert one rollout path into stable thematic source records."""
        relative_path = path.relative_to(self._sessions_root)
        messages = _read_session_messages(path)
        if not messages:
            return []
        locator_base = f"codex-session://{relative_path}"
        session_kind = _session_kind(path)
        locator_base = f"{session_kind}-session://{relative_path}"
        session_id = f"{session_kind}-session-{_stable_path_id(relative_path)}"
        occurred_on = next(
            (message.occurred_on for message in messages if message.occurred_on),
            source_date_from_locator(locator_base),
        )
        records: list[SourceRecord] = []
        for segment_number, segment in enumerate(_segment_messages(messages)):
            first = segment[0].event_index
            last = segment[-1].event_index
            segment_id = f"{session_id}-segment-{first:06d}-{last:06d}"
            locator = f"{locator_base}#segment-{first:06d}-{last:06d}"
            records.append(
                SourceRecord(
                    source_id=segment_id,
                    title=_experience_title(path, segment_number, segment),
                    content=_render_segment(segment),
                    space_id=self._space_id,
                    locator=locator,
                    occurred_on=occurred_on,
                    session_id=session_id,
                    segment_id=segment_id,
                    event_start=first,
                    event_end=last,
                )
            )
        return records

    def is_closed(self, path: Path, now: datetime | None = None) -> bool:
        """Return whether a session has been inactive long enough to ingest."""
        now = now or datetime.now(UTC)
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return (now - modified).total_seconds() >= self._closed_after_seconds


def _read_session_messages(path: Path) -> list[SessionMessage]:
    """Collect conversational messages and tool outcomes from agent JSONL."""
    messages: list[SessionMessage] = []
    tool_names: dict[str, str] = {}
    with path.open(encoding="utf-8") as source_file:
        for event_index, line in enumerate(source_file):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if event.get("type") == "response_item":
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                if payload_type in {
                    "custom_tool_call",
                    "function_call",
                    "tool_use",
                }:
                    call_id = str(payload.get("call_id") or payload.get("id") or "")
                    name = str(payload.get("name") or payload.get("tool") or "tool")
                    if call_id:
                        tool_names[call_id] = name
                    input_value = payload.get("input") or payload.get("arguments")
                    messages.append(
                        SessionMessage(
                            event_index,
                            "tool",
                            _tool_call_text(name, input_value),
                            _event_date(event.get("timestamp")),
                        )
                    )
                    continue
                if payload_type in {
                    "custom_tool_call_output",
                    "function_call_output",
                    "tool_result",
                }:
                    call_id = str(
                        payload.get("call_id")
                        or payload.get("tool_call_id")
                        or ""
                    )
                    output = payload.get("output") or payload.get("content")
                    messages.append(
                        SessionMessage(
                            event_index,
                            "tool",
                            _tool_result_text(
                                tool_names.get(call_id, "tool"),
                                output,
                            ),
                            _event_date(event.get("timestamp")),
                        )
                    )
                    continue
                if payload_type != "message":
                    continue
                message = payload
            elif event.get("type") in ("user", "assistant"):
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
            else:
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _content_to_text(message.get("content"))
            if text:
                messages.append(
                    SessionMessage(
                        event_index,
                        role,
                        text,
                        _event_date(event.get("timestamp")),
                    )
                )
    return messages


def _tool_call_text(name: str, input_value: Any) -> str:
    """Render a bounded tool invocation for extraction context."""
    if isinstance(input_value, (dict, list)):
        input_text = json.dumps(input_value, sort_keys=True)
    else:
        input_text = str(input_value or "")
    return f"tool_call name={name}: {input_text[:3000]}"


def _tool_result_text(name: str, output: Any) -> str:
    """Render a bounded tool result with an explicit exit-code signal."""
    if isinstance(output, (dict, list)):
        output_text = json.dumps(output, sort_keys=True)
    else:
        output_text = str(output or "")
    exit_match = re.search(r'"exit_code"\s*:\s*(-?\d+)', output_text)
    exit_code = exit_match.group(1) if exit_match else "unknown"
    return f"tool_result name={name} exit_code={exit_code}: {output_text[:5000]}"


def _session_kind(path: Path) -> str:
    """Identify the local agent format without relying on the root path."""
    with path.open(encoding="utf-8") as source_file:
        for line in source_file:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "response_item":
                return "codex"
            if event.get("type") in ("user", "assistant"):
                return "claude"
    return "agent"


def _event_date(timestamp: Any) -> date | None:
    """Parse an optional ISO timestamp from a Claude event."""
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _segment_messages(messages: list[SessionMessage]) -> list[list[SessionMessage]]:
    """Split clearly unrelated message windows without model-dependent IDs."""
    segments: list[list[SessionMessage]] = []
    current: list[SessionMessage] = []
    current_tokens: set[str] = set()
    for message in messages:
        tokens = _topic_tokens(message.text)
        if (
            current
            and message.role == "user"
            and len(current) >= 4
            and tokens
            and not current_tokens.intersection(tokens)
        ):
            segments.append(current)
            current = []
            current_tokens = set()
        current.append(message)
        current_tokens.update(tokens)
    if current:
        segments.append(current)
    windows: list[list[SessionMessage]] = []
    for segment in segments:
        windows.extend(_bounded_windows(segment))
    return windows


def _bounded_windows(messages: list[SessionMessage]) -> list[list[SessionMessage]]:
    """Bound thematic segments while retaining a small context overlap."""
    if (
        len(messages) <= _MAX_WINDOW_MESSAGES
        and _render_length(messages) <= _MAX_WINDOW_CHARS
    ):
        return [messages]
    windows: list[list[SessionMessage]] = []
    start = 0
    while start < len(messages):
        end = start
        total_chars = 0
        while end < len(messages) and end - start < _MAX_WINDOW_MESSAGES:
            message_length = len(messages[end].text)
            if end > start and total_chars + message_length > _MAX_WINDOW_CHARS:
                break
            total_chars += message_length
            end += 1
        if end == start:
            end += 1
        windows.append(messages[start:end])
        if end >= len(messages):
            break
        start = max(start + 1, end - _WINDOW_OVERLAP)
    return windows


def _experience_title(
    path: Path,
    segment_number: int,
    segment: list[SessionMessage],
) -> str:
    """Build a bounded title from the first user prompt."""
    prompt = next((message.text for message in segment if message.role == "user"), "")
    prompt = re.sub(r"\s+", " ", prompt).strip()
    if not prompt:
        prompt = path.stem.replace("rollout-", "Codex session")
    return f"{prompt[:100]} (experience {segment_number + 1})"


def _render_segment(segment: list[SessionMessage]) -> str:
    """Render a segment while retaining event boundaries for provenance."""
    return "\n\n".join(
        f"{message.role} event={message.event_index}: {message.text}"
        for message in segment
    )


def _render_length(messages: list[SessionMessage]) -> int:
    """Return the rendered character count for one message window."""
    return sum(len(message.text) + len(message.role) + 16 for message in messages)


def _topic_tokens(value: str) -> set[str]:
    """Return content-bearing tokens used only for deterministic segmentation."""
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", value.lower())
        if token not in _STOP_WORDS
    }


def _stable_path_id(path: Path) -> str:
    """Create a stable identifier without retaining the local absolute path."""
    return hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()[:16]


def _content_to_text(content: Any) -> str:
    """Normalize string and text-block content from local agent messages."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return text if isinstance(text, str) else ""
    if not isinstance(content, list):
        return ""

    fragments: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content")
        if isinstance(text, str):
            fragments.append(text)
    return "\n".join(fragments)
