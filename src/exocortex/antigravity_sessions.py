"""Read-only adapter for local Antigravity transcript files and experiences."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from exocortex.ingest import SourceRecord

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
class AntigravityMessage:
    """One message or tool outcome from an Antigravity transcript."""

    event_index: int
    role: str
    text: str
    occurred_on: date | None = None


class AntigravitySessionAdapter:
    """Convert local Antigravity transcript JSON Lines files into source records."""

    def __init__(
        self,
        transcripts_root: Path,
        space_id: str,
        closed_after_seconds: int = 1800,
    ) -> None:
        """Configure an explicit read-only transcripts directory."""
        self._transcripts_root = Path(transcripts_root)
        self._space_id = space_id
        self._closed_after_seconds = closed_after_seconds

    def session_paths(self, only_closed: bool = False) -> list[Path]:
        """Return eligible Antigravity transcript paths in deterministic order."""
        if not self._transcripts_root.exists():
            return []
        
        paths: list[Path] = []
        for path in self._transcripts_root.rglob("*.jsonl"):
            name = path.name.lower()
            if "transcript" in name and "full" not in name:
                paths.append(path)
            elif "transcript" in name and not paths:
                paths.append(path)
        
        # Deduplicate and sort
        unique_paths = sorted(set(paths))
        if only_closed:
            unique_paths = [p for p in unique_paths if self.is_closed(p)]
        return unique_paths

    def records(self, only_closed: bool = False) -> Iterable[SourceRecord]:
        """Yield one source record per stable thematic experience."""
        for path in self.session_paths(only_closed=only_closed):
            yield from self.records_for_path(path)

    def records_for_path(self, path: Path) -> list[SourceRecord]:
        """Convert one transcript file into stable thematic source records."""
        messages = _read_antigravity_messages(path)
        if not messages:
            return []

        conv_id = _extract_conversation_id(path, self._transcripts_root)
        session_id = f"antigravity-session-{conv_id}"
        locator_base = f"antigravity-session://{conv_id}"

        occurred_on = next(
            (m.occurred_on for m in messages if m.occurred_on),
            _path_mtime_date(path),
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
                    title=_experience_title(conv_id, segment_number, segment),
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
        """Return whether a transcript has been inactive long enough to ingest."""
        now = now or datetime.now(UTC)
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return (now - modified).total_seconds() >= self._closed_after_seconds


def _extract_conversation_id(path: Path, root: Path) -> str:
    """Extract a clean conversation ID from path."""
    try:
        rel_parts = path.relative_to(root).parts
        if len(rel_parts) >= 1:
            return rel_parts[0]
    except ValueError:
        pass
    parts = path.parts
    for i, part in enumerate(parts):
        if part in (".system_generated", "logs") and i > 0:
            return parts[i - 1]
    return path.stem


def _clean_user_content(content: str) -> str:
    """Strip Antigravity XML wrapper tags like USER_REQUEST and metadata."""
    if not content:
        return ""
    if "<USER_REQUEST>" in content:
        match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
        if match:
            return match.group(1).strip()
    # Strip other common metadata blocks
    cleaned = re.sub(
        r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, 
        flags=re.DOTALL
    )
    cleaned = re.sub(
        r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", cleaned, 
        flags=re.DOTALL
    )
    return cleaned.strip()


def _read_antigravity_lines(lines: Iterable[str]) -> list[AntigravityMessage]:
    """Collect conversational messages and tool invocations from lines of JSONL."""
    messages: list[AntigravityMessage] = []
    last_tool_name = "tool"

    for event_index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            created_at = _parse_date(event.get("created_at"))

            # 1. User Inputs
            if event_type == "USER_INPUT":
                content = _clean_user_content(str(event.get("content") or ""))
                if content:
                    messages.append(
                        AntigravityMessage(
                            event_index=event_index,
                            role="user",
                            text=content,
                            occurred_on=created_at,
                        )
                    )

            # 2. Assistant Responses & Tool Calls
            elif event_type == "PLANNER_RESPONSE":
                tool_calls = event.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            name = str(tc.get("name") or "tool")
                            last_tool_name = name
                            args = tc.get("args")
                            messages.append(
                                AntigravityMessage(
                                    event_index=event_index,
                                    role="tool",
                                    text=_tool_call_text(name, args),
                                    occurred_on=created_at,
                                )
                            )
                content = str(event.get("content") or "").strip()
                if content:
                    messages.append(
                        AntigravityMessage(
                            event_index=event_index,
                            role="assistant",
                            text=content,
                            occurred_on=created_at,
                        )
                    )

            # 3. Tool outputs / Generic execution steps
            elif event_type == "GENERIC":
                content = str(event.get("content") or "").strip()
                if content:
                    messages.append(
                        AntigravityMessage(
                            event_index=event_index,
                            role="tool",
                            text=_tool_result_text(last_tool_name, content),
                            occurred_on=created_at,
                        )
                    )

    return messages


def _parse_date(val: Any) -> date | None:
    """Extract a calendar date from ISO 8601 string."""
    if not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _path_mtime_date(path: Path) -> date:
    """Return mtime date of file."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()


def _tool_call_text(name: str, input_value: Any) -> str:
    """Render a bounded tool invocation for extraction context."""
    if isinstance(input_value, (dict, list)):
        input_text = json.dumps(input_value, sort_keys=True)
    else:
        input_text = str(input_value or "")
    return f"tool_call name={name}: {input_text[:3000]}"


def _tool_result_text(name: str, output: Any) -> str:
    """Render bounded tool execution output."""
    output_text = str(output or "").strip()
    return f"tool_result name={name}: {output_text[:3000]}"


def _segment_messages(
    messages: list[AntigravityMessage],
) -> list[list[AntigravityMessage]]:
    """Segment messages into bounded, overlapping thematic experiences."""
    if not messages:
        return []

    segments: list[list[AntigravityMessage]] = []
    current_segment: list[AntigravityMessage] = []
    current_chars = 0

    for msg in messages:
        msg_chars = len(msg.text)
        if current_segment and (
            len(current_segment) >= _MAX_WINDOW_MESSAGES
            or (current_chars + msg_chars) > _MAX_WINDOW_CHARS
        ):
            segments.append(list(current_segment))
            # Overlap last N messages
            has_overlap = len(current_segment) > _WINDOW_OVERLAP
            overlap = current_segment[-_WINDOW_OVERLAP:] if has_overlap else []
            current_segment = list(overlap)
            current_chars = sum(len(m.text) for m in current_segment)

        current_segment.append(msg)
        current_chars += msg_chars

    if current_segment:
        segments.append(current_segment)

    return segments


def _render_segment(segment: list[AntigravityMessage]) -> str:
    """Format an experience segment as readable Markdown dialog."""
    lines: list[str] = []
    for msg in segment:
        lines.append(f"### {msg.role.capitalize()}\n{msg.text}\n")
    return "\n".join(lines).strip()


def _experience_title(
    conv_id: str,
    segment_number: int,
    segment: list[AntigravityMessage],
) -> str:
    """Produce a concise title for the segment."""
    for msg in segment:
        if msg.role == "user" and msg.text:
            first_line = msg.text.split("\n", 1)[0].strip()
            first_line = re.sub(r"[#*`_]+", "", first_line)
            words = [w for w in first_line.split() if w.lower() not in _STOP_WORDS]
            candidate = " ".join(words[:8]).strip()
            if candidate:
                return candidate[:80]

    return f"Antigravity session {conv_id[:8]} segment {segment_number + 1}"



def _read_antigravity_messages(path: Path) -> list[AntigravityMessage]:
    """Collect conversational messages from transcript file."""
    with path.open(encoding="utf-8") as source_file:
        return _read_antigravity_lines(source_file)


def parse_antigravity_records(
    lines: Iterable[str],
    conversation_id: str,
    space_id: str = "work",
    occurred_on: date | None = None,
) -> list[SourceRecord]:
    """Convert transcript JSONL lines into stable thematic source records."""
    messages = _read_antigravity_lines(lines)
    if not messages:
        return []

    session_id = f"antigravity-session-{conversation_id}"
    locator_base = f"antigravity-session://{conversation_id}"
    date_val = occurred_on or next(
        (m.occurred_on for m in messages if m.occurred_on),
        date.today(),
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
                title=_experience_title(conversation_id, segment_number, segment),
                content=_render_segment(segment),
                space_id=space_id,
                locator=locator,
                occurred_on=date_val,
                session_id=session_id,
                segment_id=segment_id,
                event_start=first,
                event_end=last,
            )
        )
    return records
