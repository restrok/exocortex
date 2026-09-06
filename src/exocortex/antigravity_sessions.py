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
_MIN_WINDOW_CHARS = 4_000
_TARGET_WINDOW_CHARS = 8_000
_MAX_WINDOW_CHARS = 14_000
_MAX_WINDOW_TURNS = 12
_WINDOW_OVERLAP = 2

_CONTINUATION_PATTERNS = re.compile(
    r"^(ok|okay|si|sí|dale|de acuerdo|perfecto|joya|avanza|avanzá|"
    r"continua|continuá|procede|procedé|seguí|seguir|hacelo|yes|"
    r"proceed|go ahead|continue|looks good|lgtm|done|next|approved|"
    r"si dale|si procedé|ok vamos|ok dale|dale vamos|si por favor|"
    r"hacelo vos)[\s.!]*$",
    re.IGNORECASE,
)

_DIRECTIVE_PATTERNS = re.compile(
    r"^(crea|creá|implementa|implementá|modifica|modificá|agrega|agregá|"
    r"cambia|cambiá|vamos con|ahora|quiero|necesitamos|tengo|tenemos|hay un error|"
    r"investiga|investigá|revisa|revisá|construye|construí|how|why|what|"
    r"create|build|implement|fix|refactor|add|update)\b",
    re.IGNORECASE,
)


@dataclass
class AntigravityTurn:
    """A semantic conversation turn preserving atomic tool executions."""

    role: str
    text: str
    event_start: int
    event_end: int
    occurred_on: date | None = None
    is_intent_shift: bool = False

    def __init__(
        self,
        role: str,
        text: str,
        event_start: int | None = None,
        event_end: int | None = None,
        event_index: int | None = None,
        occurred_on: date | None = None,
        is_intent_shift: bool = False,
    ) -> None:
        self.role = role
        self.text = text
        start = (
            event_start
            if event_start is not None
            else (event_index if event_index is not None else 0)
        )
        self.event_start = start
        self.event_end = event_end if event_end is not None else start
        self.occurred_on = occurred_on
        self.is_intent_shift = is_intent_shift

    @property
    def event_index(self) -> int:
        """Legacy compatibility property matching event_start."""
        return self.event_start


AntigravityMessage = AntigravityTurn


class AntigravitySessionAdapter:
    """Convert local Antigravity transcript JSON Lines files into source records."""

    def __init__(
        self,
        transcripts_root: Path,
        space_id: str,
        closed_after_seconds: int = 1800,
        gateway: Any = None,
    ) -> None:
        """Configure an explicit read-only transcripts directory."""
        self._transcripts_root = Path(transcripts_root)
        self._space_id = space_id
        self._closed_after_seconds = closed_after_seconds
        self._gateway = gateway

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
        turns = _read_antigravity_messages(path)
        if not turns:
            return []

        conv_id = _extract_conversation_id(path, self._transcripts_root)
        session_id = f"antigravity-session-{conv_id}"
        locator_base = f"antigravity-session://{conv_id}"

        occurred_on = next(
            (m.occurred_on for m in turns if m.occurred_on),
            _path_mtime_date(path),
        )

        records: list[SourceRecord] = []
        for segment_number, segment in enumerate(
            _segment_turns(turns, gateway=self._gateway)
        ):
            first = segment[0].event_start
            last = segment[-1].event_end
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
    cleaned = re.sub(
        r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, flags=re.DOTALL
    )
    cleaned = re.sub(
        r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>",
        "",
        cleaned,
        flags=re.DOTALL,
    )
    return cleaned.strip()


def _is_user_intent_shift(content: str) -> bool:
    """Classify if a user message represents a substantive intent change."""
    cleaned = content.strip()
    if not cleaned:
        return False
    if _CONTINUATION_PATTERNS.match(cleaned):
        return False
    if len(cleaned) > 100:
        return True
    if _DIRECTIVE_PATTERNS.search(cleaned):
        return True
    if "```" in cleaned or "\n- " in cleaned or "\n1. " in cleaned:
        return True
    return False


def _compact_payload(
    text: str,
    max_head: int = 500,
    max_tail: int = 500,
    max_total: int = 1500,
) -> str:
    """Intelligently compact large diffs, terminal outputs, and logs."""
    text = text.strip()
    if len(text) <= max_total:
        return text

    lines = text.splitlines()
    is_diff = any(
        line.startswith("diff --git") or line.startswith("@@")
        for line in lines[:10]
    )
    if is_diff and len(lines) > 20:
        head_lines = lines[:10]
        tail_lines = lines[-10:]
        omitted = len(lines) - 20
        return (
            "\n".join(head_lines)
            + f"\n... [omitted {omitted} diff lines] ...\n"
            + "\n".join(tail_lines)
        )

    head = text[:max_head].rstrip()
    tail = text[-max_tail:].lstrip()
    omitted_chars = len(text) - (len(head) + len(tail))
    return f"{head}\n... [omitted {omitted_chars} characters] ...\n{tail}"


def _read_antigravity_lines(lines: Iterable[str]) -> list[AntigravityTurn]:
    """Collect conversational messages and atomic tool executions from JSONL."""
    turns: list[AntigravityTurn] = []
    pending_tools: list[tuple[int, str, Any, date | None]] = []
    last_tool_name = "tool"

    def flush_pending_tools() -> None:
        while pending_tools:
            call_idx, name, args, created = pending_tools.pop(0)
            compacted_args = _compact_payload(
                json.dumps(args, sort_keys=True)
                if isinstance(args, (dict, list))
                else str(args or ""),
                max_total=800,
            )
            text = f"tool_call name={name} (no output recorded):\n{compacted_args}"
            turns.append(
                AntigravityTurn(
                    role="tool",
                    text=text,
                    event_start=call_idx,
                    event_end=call_idx,
                    occurred_on=created,
                )
            )

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
            flush_pending_tools()
            content = _clean_user_content(str(event.get("content") or ""))
            if content:
                is_shift = _is_user_intent_shift(content)
                turns.append(
                    AntigravityTurn(
                        role="user",
                        text=content,
                        event_start=event_index,
                        event_end=event_index,
                        occurred_on=created_at,
                        is_intent_shift=is_shift,
                    )
                )

        # 2. Assistant Responses & Tool Calls
        elif event_type == "PLANNER_RESPONSE":
            content = str(event.get("content") or "").strip()
            if content:
                flush_pending_tools()
                turns.append(
                    AntigravityTurn(
                        role="assistant",
                        text=content,
                        event_start=event_index,
                        event_end=event_index,
                        occurred_on=created_at,
                    )
                )

            tool_calls = event.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = str(tc.get("name") or "tool")
                        last_tool_name = name
                        args = tc.get("args")
                        pending_tools.append((event_index, name, args, created_at))

        # 3. Tool outputs / Generic execution steps
        elif event_type == "GENERIC":
            content = str(event.get("content") or "").strip()
            if not content:
                continue

            if pending_tools:
                call_idx, name, args, call_created = pending_tools.pop(0)
                compacted_args = _compact_payload(
                    json.dumps(args, sort_keys=True)
                    if isinstance(args, (dict, list))
                    else str(args or ""),
                    max_total=800,
                )
                compacted_result = _compact_payload(
                    content,
                    max_head=600,
                    max_tail=600,
                    max_total=1800,
                )
                text = (
                    f"tool_execution name={name}\n"
                    f"call: {compacted_args}\n"
                    f"output:\n{compacted_result}"
                )
                turns.append(
                    AntigravityTurn(
                        role="tool",
                        text=text,
                        event_start=call_idx,
                        event_end=event_index,
                        occurred_on=call_created or created_at,
                    )
                )
            else:
                compacted_result = _compact_payload(
                    content,
                    max_head=600,
                    max_tail=600,
                    max_total=1800,
                )
                turns.append(
                    AntigravityTurn(
                        role="tool",
                        text=f"tool_result name={last_tool_name}:\n{compacted_result}",
                        event_start=event_index,
                        event_end=event_index,
                        occurred_on=created_at,
                    )
                )

    flush_pending_tools()
    return turns


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


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _segment_turns(
    turns: list[AntigravityTurn],
    gateway: Any = None,
) -> list[list[AntigravityTurn]]:
    """Segment turns into bounded, semantically cohesive experiences."""
    if not turns:
        return []

    segments: list[list[AntigravityTurn]] = []
    current_segment: list[AntigravityTurn] = []
    current_chars = 0

    turn_embeddings: dict[int, list[float]] = {}
    if gateway and hasattr(gateway, "embed_batch"):
        try:
            texts_to_embed = [
                turn.text[:400]
                for turn in turns
                if turn.role in ("user", "assistant") or turn.is_intent_shift
            ]
            if texts_to_embed:
                embeddings = gateway.embed_batch(texts_to_embed, timeout_seconds=10)
                emb_idx = 0
                for i, turn in enumerate(turns):
                    if turn.role in ("user", "assistant") or turn.is_intent_shift:
                        if emb_idx < len(embeddings):
                            turn_embeddings[i] = embeddings[emb_idx]
                            emb_idx += 1
        except Exception:
            turn_embeddings = {}

    for turn_idx, turn in enumerate(turns):
        turn_chars = len(turn.text)
        should_cut = False

        if current_segment and current_chars >= _MIN_WINDOW_CHARS:
            # 1. User Intent Shift Boundary
            if turn.role == "user" and turn.is_intent_shift:
                should_cut = True

            # 2. Semantic drift valley
            elif turn_idx in turn_embeddings and (turn_idx - 1) in turn_embeddings:
                sim = _cosine_similarity(
                    turn_embeddings[turn_idx - 1],
                    turn_embeddings[turn_idx],
                )
                if sim < 0.60 and current_chars >= _TARGET_WINDOW_CHARS:
                    should_cut = True

            # 3. Maximum limits
            elif (
                len(current_segment) >= _MAX_WINDOW_TURNS
                or (current_chars + turn_chars) > _MAX_WINDOW_CHARS
            ):
                should_cut = True

        elif current_segment and (
            len(current_segment) >= _MAX_WINDOW_TURNS
            or (current_chars + turn_chars) > _MAX_WINDOW_CHARS
        ):
            should_cut = True

        if should_cut:
            segments.append(list(current_segment))
            has_overlap = len(current_segment) > _WINDOW_OVERLAP
            overlap = current_segment[-_WINDOW_OVERLAP:] if has_overlap else []
            current_segment = list(overlap)
            current_chars = sum(len(t.text) for t in current_segment)

        current_segment.append(turn)
        current_chars += turn_chars

    if current_segment:
        segments.append(current_segment)

    return segments


def _segment_messages(
    messages: list[AntigravityTurn],
) -> list[list[AntigravityTurn]]:
    """Legacy compatibility entrypoint delegating to _segment_turns."""
    return _segment_turns(messages)


def _render_segment(segment: list[AntigravityTurn]) -> str:
    """Format an experience segment as readable Markdown dialog."""
    lines: list[str] = []
    for turn in segment:
        lines.append(f"### {turn.role.capitalize()}\n{turn.text}\n")
    return "\n".join(lines).strip()


def _experience_title(
    conv_id: str,
    segment_number: int,
    segment: list[AntigravityTurn],
) -> str:
    """Produce a concise title for the segment."""
    for turn in segment:
        if turn.role == "user" and turn.text:
            first_line = turn.text.split("\n", 1)[0].strip()
            first_line = re.sub(r"[#*`_]+", "", first_line)
            words = [w for w in first_line.split() if w.lower() not in _STOP_WORDS]
            candidate = " ".join(words[:8]).strip()
            if candidate:
                return candidate[:80]

    return f"Antigravity session {conv_id[:8]} segment {segment_number + 1}"


def _read_antigravity_messages(path: Path) -> list[AntigravityTurn]:
    """Collect conversational turns from transcript file."""
    with path.open(encoding="utf-8") as source_file:
        return _read_antigravity_lines(source_file)


def parse_antigravity_records(
    lines: Iterable[str],
    conversation_id: str,
    space_id: str = "work",
    occurred_on: date | None = None,
    gateway: Any = None,
) -> list[SourceRecord]:
    """Convert transcript JSONL lines into stable thematic source records."""
    turns = _read_antigravity_lines(lines)
    if not turns:
        return []

    session_id = f"antigravity-session-{conversation_id}"
    locator_base = f"antigravity-session://{conversation_id}"
    date_val = occurred_on or next(
        (m.occurred_on for m in turns if m.occurred_on),
        date.today(),
    )
    records: list[SourceRecord] = []
    for segment_number, segment in enumerate(_segment_turns(turns, gateway=gateway)):
        first = segment[0].event_start
        last = segment[-1].event_end
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
