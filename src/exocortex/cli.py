"""Typer command-line interface for Codex Brain."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated, TextIO
from urllib.parse import urlsplit, urlunsplit

import typer

from exocortex.config import Settings
from exocortex.models import ResponseEnvelope
from exocortex.service import BrainService
from exocortex.telemetry import flush_telemetry

app = typer.Typer(help="Obsidian-first knowledge brain controls.")
config_app = typer.Typer(help="Create or inspect project configuration.")
review_app = typer.Typer(help="Promote or reject staged knowledge.")
repair_app = typer.Typer(help="Repair and roll back canonical Brain data.")
app.add_typer(config_app, name="config")
app.add_typer(review_app, name="review")
app.add_typer(repair_app, name="repair")

_CLAUDE_MCP_NAME = "codex-brain"
_CLAUDE_MEMORY_PERMISSION = "mcp__codex-brain__brain_remember"


def _service() -> BrainService:
    """Create a service using the active environment configuration."""
    return BrainService(Settings())


def _emit_response(
    status: str,
    method: str,
    data: object,
    meta: dict[str, object] | None = None,
) -> None:
    """Print one schema-v2 response envelope."""
    typer.echo(
        ResponseEnvelope(
            status=status,
            method=method,
            data=data,
            meta=meta or {},
        ).model_dump_json(indent=2)
    )


@app.command()
def init() -> None:
    """Create local directories and initialize the Neo4j schema."""
    service = _service()
    service.initialize()
    _emit_response("ok", "init", {"message": "Initialized Vault and Neo4j schema."})


@app.command()
def doctor() -> None:
    """Report health of Vault, gateway, and Neo4j without leaking secrets."""
    report = _service().doctor()
    _emit_response(
        "ok" if "unavailable" not in (report.gateway, report.neo4j) else "degraded",
        "health",
        {
            "vault": report.vault,
            "gateway": report.gateway,
            "neo4j": report.neo4j,
            "detail": report.detail,
        },
    )
    if "unavailable" in (report.gateway, report.neo4j):
        raise typer.Exit(code=1)


@app.command()
def ingest(
    source: Annotated[Path, typer.Argument(exists=True, readable=True)],
    space: Annotated[str | None, typer.Option()] = None,
    extract: Annotated[bool, typer.Option()] = True,
) -> None:
    """Ingest a source file using the portable text, JSON, or JSONL contract."""
    service = _service()
    results = service._ingestor().ingest_file(  # pylint: disable=protected-access
        source,
        space or service.settings.default_space,
        extract=extract,
    )
    _emit_response("ok", "ingest", [result.__dict__ for result in results])


@app.command("ingest-codex")
def ingest_codex(
    sessions_root: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    space: Annotated[str | None, typer.Option()] = None,
    extract: Annotated[bool, typer.Option()] = True,
    max_llm_calls: Annotated[int | None, typer.Option(min=0)] = None,
    max_seconds: Annotated[int | None, typer.Option(min=0)] = None,
    batch_size: Annotated[int | None, typer.Option(min=1, max=50)] = None,
) -> None:
    """Ingest local Codex rollouts with bounded, resumable extraction."""
    service = _service()
    results = service.ingest_codex(
        sessions_root=sessions_root,
        extract=extract,
        space_id=space,
        max_llm_calls=max_llm_calls,
        max_seconds=max_seconds,
        batch_size=batch_size,
    )
    _emit_response("ok", "ingest-codex", [result.__dict__ for result in results])


@app.command("ingest-antigravity")
def ingest_antigravity(
    transcripts_root: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    space: Annotated[str | None, typer.Option()] = None,
    extract: Annotated[bool, typer.Option()] = True,
    max_llm_calls: Annotated[int | None, typer.Option(min=0)] = None,
    max_seconds: Annotated[int | None, typer.Option(min=0)] = None,
    batch_size: Annotated[int | None, typer.Option(min=1, max=50)] = None,
) -> None:
    """Ingest local Antigravity transcripts with bounded, resumable extraction."""
    service = _service()
    results = service.ingest_antigravity(
        transcripts_root=transcripts_root,
        extract=extract,
        space_id=space,
        max_llm_calls=max_llm_calls,
        max_seconds=max_seconds,
        batch_size=batch_size,
    )
    _emit_response("ok", "ingest-antigravity", [result.__dict__ for result in results])


@app.command()
def sync(
    embed: Annotated[bool, typer.Option()] = True,
) -> None:
    """Project all canonical Vault notes into Neo4j."""
    count = _service().sync(embed=embed)
    _emit_response("ok", "sync", {"indexed": count})


@app.command()
def search(
    query: Annotated[str, typer.Argument()],
    space: Annotated[str | None, typer.Option()] = None,
    project: Annotated[str | None, typer.Option()] = None,
    repository: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=25)] = 5,
    answer_mode: Annotated[str, typer.Option()] = "conservative",
    include_candidates: Annotated[bool, typer.Option()] = False,
) -> None:
    """Return grounded Markdown and source citations for a natural-language query."""
    typer.echo(
        _service()
        .search_response(
            query,
            space_id=space,
            project_id=project,
            repository_id=repository,
            limit=limit,
            answer_mode=answer_mode,
            include_candidates=include_candidates,
        )
        .model_dump_json(indent=2)
    )


@app.command("list-by-label")
def list_by_label(
    labels: Annotated[list[str], typer.Argument()],
    space: Annotated[str | None, typer.Option()] = None,
    match_all: Annotated[bool, typer.Option()] = False,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 25,
) -> None:
    """List canonical notes matching one or more labels."""
    results = _service().list_by_label(
        labels,
        space_id=space,
        match_all=match_all,
        limit=limit,
    )
    _emit_response(
        "ok" if results else "not_found",
        "labels",
        [result.model_dump(mode="json") for result in results],
    )


@app.command("reflect")
def reflect(
    limit: Annotated[int | None, typer.Option(min=2, max=250)] = None,
) -> None:
    """Consolidate changed experiences into validated workflows."""
    _emit_response("ok", "reflect", _service().reflect(limit=limit))


@app.command("learning-status")
def learning_status() -> None:
    """Show non-sensitive reflection progress."""
    _emit_response("ok", "learning-status", _service().learning_status())


@app.command("ingest-status")
def ingest_status() -> None:
    """Show current Codex rollout ingestion progress without calling the gateway."""
    _emit_response("ok", "ingest-status", _service().ingest_status())


@app.command("extraction-canary")
def extraction_canary() -> None:
    """Validate a single synthetic batch against the full extraction contract."""
    try:
        result = _service().extraction_canary()
    except Exception as error:  # pylint: disable=broad-except
        _emit_response(
            "degraded",
            "extraction-canary",
            {"status": "failed", "reason": error.__class__.__name__},
        )
        raise typer.Exit(code=1) from error
    _emit_response("ok", "extraction-canary", result)


@app.command("labels")
def labels() -> None:
    """Show the non-sensitive canonical label aliases."""
    _emit_response("ok", "labels", _service().labels.aliases())


@app.command("timeline")
def timeline(
    start_on: Annotated[str, typer.Option("--from")],
    end_on: Annotated[str, typer.Option("--to")],
    space: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option(min=1, max=250)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """List canonical notes by the date of their source conversations."""
    try:
        start_date = date.fromisoformat(start_on)
        end_date = date.fromisoformat(end_on)
    except ValueError as error:
        raise typer.BadParameter("Dates must use YYYY-MM-DD.") from error
    service = _service()
    coverage = service.date_coverage(start_date, end_date, space_id=space)
    results = service.notes_by_date(
        start_date,
        end_date,
        space_id=space,
        limit=limit,
        offset=offset,
    )
    _emit_response(
        "ok" if results else "not_found",
        "timeline",
        [result.model_dump(mode="json") for result in results],
        {
            "limit": limit,
            "offset": offset,
            "result_count": len(results),
            "total_count": coverage["notes_in_range"],
            "has_more": offset + len(results) < coverage["notes_in_range"],
            "next_offset": (
                offset + len(results)
                if offset + len(results) < coverage["notes_in_range"]
                else None
            ),
            "date_basis": "source_reference.occurred_on",
            "coverage": coverage,
        },
    )


@app.command()
def rebuild() -> None:
    """Recreate Neo4j from canonical Markdown after an explicit operator command."""
    count = _service().rebuild()
    _emit_response("ok", "rebuild", {"indexed": count})


@repair_app.command("report")
def repair_report() -> None:
    """Report deterministic data repairs without changing the Vault."""
    _emit_response("ok", "repair-report", _service().repair_report())


@repair_app.command("apply")
def repair_apply() -> None:
    """Back up and apply deterministic data repairs."""
    _emit_response("ok", "repair-apply", _service().repair_apply())


@repair_app.command("rollback")
def repair_rollback(
    backup: Annotated[Path, typer.Argument(exists=True, readable=True)],
) -> None:
    """Restore canonical data from one repair backup."""
    _emit_response("ok", "repair-rollback", _service().repair_rollback(backup))


@app.command()
def backfill(
    all_sources: Annotated[bool, typer.Option("--all")] = False,
    batch_size: Annotated[int, typer.Option(min=1, max=250)] = 25,
    resume: Annotated[bool, typer.Option()] = True,
    max_failures: Annotated[int, typer.Option(min=0, max=250)] = 25,
) -> None:
    """Re-extract sources with logs, checkpoints, and a failure circuit breaker."""
    result = _service().backfill(
        batch_size=batch_size,
        resume=resume,
        process_all=all_sources,
        max_failures=max_failures,
    )
    _emit_response(
        "ok" if result.get("status") == "completed" else "degraded",
        "backfill",
        result,
    )
    if result.get("status") != "completed":
        raise typer.Exit(code=1)


@app.command("retry-fallbacks")
def retry_fallbacks(
    all_sources: Annotated[bool, typer.Option("--all")] = False,
    batch_size: Annotated[int, typer.Option(min=1, max=250)] = 25,
    max_failures: Annotated[int, typer.Option(min=0, max=250)] = 25,
) -> None:
    """Re-extract only notes identified as deterministic fallbacks."""
    result = _service().retry_fallbacks(
        batch_size=batch_size,
        process_all=all_sources,
        max_failures=max_failures,
    )
    _emit_response(
        "ok" if result.get("status") == "completed" else "degraded",
        "retry-fallbacks",
        result,
    )
    if result.get("status") != "completed":
        raise typer.Exit(code=1)


@app.command("eval")
def evaluate_quality(
    data: Annotated[Path, typer.Option()] = Path("evals/golden-v1.jsonl"),
    generate: Annotated[bool, typer.Option()] = False,
    live: Annotated[bool, typer.Option()] = False,
    enforce_gates: Annotated[bool, typer.Option()] = False,
) -> None:
    """Generate or run the frozen golden-set quality evaluation."""
    from exocortex.evaluation import evaluate, generate_cases, load_cases

    service = _service()
    if generate:
        if data.exists():
            raise typer.BadParameter(f"Golden set already exists: {data}")
        data.parent.mkdir(parents=True, exist_ok=True)
        cases = generate_cases(service)
        data.write_text(
            "\n".join(json.dumps(case, sort_keys=True) for case in cases) + "\n",
            encoding="utf-8",
        )
        _emit_response("ok", "eval-generate", {"cases": len(cases)})
        return
    if live:
        health = service.doctor()
        if health.gateway != "ok" or health.neo4j != "ok":
            _emit_response("degraded", "eval", {"reason": health.detail})
            if enforce_gates:
                raise typer.Exit(code=2)
            return
    result = evaluate(service, load_cases(data), live=live)
    _emit_response(
        "ok" if all(result["gates"].values()) else "degraded",
        "eval",
        result,
    )
    if enforce_gates and not all(result["gates"].values()):
        raise typer.Exit(code=2)


@app.command()
def audit() -> None:
    """Fail when persisted Markdown still contains secret-like values."""
    findings = _service().audit()
    _emit_response(
        "ok" if not findings else "degraded",
        "audit",
        {"findings": findings},
    )
    if findings:
        raise typer.Exit(code=1)


@app.command("export")
def export_brain(
    output_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Create a compressed export of Vault and sanitized source evidence."""
    output_path = _service().export(output_dir)
    _emit_response("ok", "export", {"path": str(output_path)})


@app.command()
def remember(
    content: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Option()] = "Captured memory",
    space: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Store a sanitized memory immediately in the canonical work Vault."""
    service = _service()
    response = service.remember_response(
        content,
        title=title,
        space_id=space or service.settings.default_space,
    )
    _emit_response(response.status, response.method, response.data)


@review_app.command("list")
def review_list() -> None:
    """List candidates requiring an explicit human decision."""
    service = _service()
    candidates = []
    for path in sorted(service.settings.review_dir.glob("*.md")):
        post = service.vault.read_review(path.stem)
        if post:
            candidates.append(
                {
                    "source_id": post.metadata.get("source_id"),
                    "space_id": post.metadata.get("space_id"),
                    "reason": post.metadata.get("reason"),
                    "path": str(path),
                }
            )
    _emit_response("ok" if candidates else "not_found", "review-list", candidates)


@review_app.command("promote")
def review_promote(
    source_id: Annotated[str, typer.Argument()],
    index: Annotated[bool, typer.Option()] = True,
) -> None:
    """Promote one review candidate into the canonical Vault."""
    note = _service().promote(source_id, index=index)
    _emit_response(
        "stored",
        "review-promote",
        {"note_id": str(note.metadata.id), "note_path": note.path},
    )


@review_app.command("reject")
def review_reject(source_id: Annotated[str, typer.Argument()]) -> None:
    """Reject one candidate while retaining only the sanitized source evidence."""
    _service().reject(source_id)
    _emit_response("ok", "review-reject", {"source_id": source_id})


@config_app.command("init")
def config_init(
    from_codex: Annotated[bool, typer.Option("--from-codex")] = False,
    output: Annotated[Path, typer.Option()] = Path(".env"),
) -> None:
    """Create a local environment file without copying Codex credentials."""
    values = {
        "BRAIN_HOST_DATA_DIR": "./brain",
        "BRAIN_DEFAULT_SPACE": "work",
        "BRAIN_LLM_BASE_URL": "https://api.openai.com/v1",
        "BRAIN_LLM_MODEL": "gpt-4o-mini",
        "BRAIN_REFLECTION_MODEL": "gpt-4o",
        "BRAIN_REFLECTION_REASONING_EFFORT": "",
        "BRAIN_EMBEDDING_MODEL": "text-embedding-3-small",
        "BRAIN_LLM_API_KEY": "",
        "BRAIN_LLM_TIMEOUT_SECONDS": "180",
        "BRAIN_GATEWAY_WALL_TIMEOUT_SECONDS": "120",
        "BRAIN_CANARY_TIMEOUT_SECONDS": "30",
        "BRAIN_GATEWAY_RETRY_ATTEMPTS": "1",
        "BRAIN_GATEWAY_RETRY_BACKOFF_SECONDS": "0.25",
        "BRAIN_EXTRACTION_MAX_CHARS": "16000",
        "BRAIN_CODEX_SESSIONS_HOST_DIR": str(Path.home() / ".codex" / "sessions"),
        "BRAIN_CODEX_SESSIONS_DIR": "/sources/codex",
        "BRAIN_SESSION_CLOSED_AFTER_SECONDS": "1800",
        "BRAIN_REFLECTION_HOUR": "3",
        "BRAIN_TIMEZONE": "America/Argentina/Buenos_Aires",
        "BRAIN_REFLECTION_MAX_NOTES": "50",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "replace-with-a-strong-local-password",
        "BRAIN_MCP_HOST": "127.0.0.1",
        "BRAIN_MCP_PORT": "8765",
        "BRAIN_SYNC_INTERVAL_SECONDS": "86400",
    }
    if from_codex:
        values.update(_read_codex_provider())
        values["BRAIN_LLM_MODEL"] = "gpt-5.6-luna"
    output.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"Wrote configuration to {output}.")


@config_app.command("install-codex")
def config_install_codex(
    mcp_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8765/mcp",
    skill_source: Annotated[Path, typer.Option()] = Path(
        "integrations/codex/codex-work-brain"
    ),
) -> None:
    """Install the local work skill and MCP registration for the current user."""
    if not skill_source.exists():
        raise typer.BadParameter(f"Skill source does not exist: {skill_source}")

    home = Path.home()
    target = home / ".agents" / "skills" / "codex-work-brain"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_source, target, dirs_exist_ok=True)

    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        config_text = config_path.read_text(encoding="utf-8")
        if _mcp_server_is_registered(config_text):
            if _mcp_tool_is_approved(config_text, "brain_remember"):
                typer.echo("Codex work brain MCP registration already exists.")
                return
            with config_path.open("a", encoding="utf-8") as config_file:
                _write_brain_remember_approval(config_file)
            typer.echo("Updated Codex work brain memory-capture approval.")
            return
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write(
            "\n[mcp_servers.codex_work_brain]\n"
            f'url = "{mcp_url}"\n'
            "startup_timeout_sec = 30\n"
            "tool_timeout_sec = 60\n"
            'default_tools_approval_mode = "writes"\n'
        )
        _write_brain_remember_approval(config_file)
    typer.echo("Installed the codex-work-brain skill and Codex MCP registration.")


@config_app.command("install-claude")
def config_install_claude(
    mcp_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8765/mcp",
    skill_source: Annotated[Path, typer.Option()] = Path(
        "integrations/codex/codex-work-brain"
    ),
) -> None:
    """Install the shared work skill and MCP registration for Claude Code."""
    skill_path = skill_source / "SKILL.md"
    if not skill_path.is_file():
        raise typer.BadParameter(f"Skill file does not exist: {skill_path}")

    _register_claude_mcp(mcp_url)

    home = Path.home()
    skill_target = home / ".claude" / "skills" / "codex-work-brain"
    skill_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_path, skill_target / "SKILL.md")

    settings_path = home / ".claude" / "settings.json"
    _ensure_claude_permission(settings_path)
    typer.echo(
        "Installed the shared codex-work-brain skill and Claude Code MCP "
        "registration."
    )


def _register_claude_mcp(mcp_url: str) -> None:
    """Register the local Brain MCP server in Claude Code when needed."""
    try:
        existing = subprocess.run(
            ["claude", "mcp", "get", _CLAUDE_MCP_NAME],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as error:
        raise typer.BadParameter(
            "Claude Code executable not found. Install Claude Code first."
        ) from error

    output = f"{existing.stdout}\n{existing.stderr}"
    if existing.returncode == 0:
        if f"URL: {mcp_url}" not in output:
            raise typer.BadParameter(
                f"Claude MCP server {_CLAUDE_MCP_NAME!r} already exists with "
                "a different URL. Remove it or pass the configured URL."
            )
        return

    try:
        subprocess.run(
            [
                "claude",
                "mcp",
                "add",
                "--transport",
                "http",
                "--scope",
                "user",
                _CLAUDE_MCP_NAME,
                mcp_url,
            ],
            check=True,
        )
    except FileNotFoundError as error:
        raise typer.BadParameter(
            "Claude Code executable not found. Install Claude Code first."
        ) from error
    except subprocess.CalledProcessError as error:
        raise typer.BadParameter(
            "Claude Code could not register the Codex Brain MCP server."
        ) from error


def _ensure_claude_permission(settings_path: Path) -> None:
    """Allow Claude Code to store memories through the local Brain MCP."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise typer.BadParameter(
                f"Claude settings are not valid JSON: {settings_path}"
            ) from error
    else:
        settings = {}

    if not isinstance(settings, dict):
        raise typer.BadParameter(
            f"Claude settings must contain a JSON object: {settings_path}"
        )

    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise typer.BadParameter(
            f"Claude permissions must contain a JSON object: {settings_path}"
        )

    allowed_tools = permissions.setdefault("allow", [])
    if not isinstance(allowed_tools, list) or not all(
        isinstance(tool, str) for tool in allowed_tools
    ):
        raise typer.BadParameter(
            f"Claude permissions.allow must be a string array: {settings_path}"
        )

    if _CLAUDE_MEMORY_PERMISSION not in allowed_tools:
        allowed_tools.append(_CLAUDE_MEMORY_PERMISSION)
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n",
            encoding="utf-8",
        )


def _mcp_server_is_registered(config_text: str) -> bool:
    """Check MCP registration without parsing optional Codex TOML extensions."""
    return any(
        line.strip() == "[mcp_servers.codex_work_brain]"
        for line in config_text.splitlines()
    )


def _mcp_tool_is_approved(config_text: str, tool_name: str) -> bool:
    """Return whether one work-brain tool has an explicit approval override."""
    header = f"[mcp_servers.codex_work_brain.tools.{tool_name}]"
    in_tool_section = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped == header:
            in_tool_section = True
            continue
        if in_tool_section and stripped.startswith("["):
            return False
        if in_tool_section and stripped == 'approval_mode = "approve"':
            return True
    return False


def _write_brain_remember_approval(config_file: TextIO) -> None:
    """Allow the local staged-memory tool without invoking Auto-review."""
    config_file.write(
        "\n[mcp_servers.codex_work_brain.tools.brain_remember]\n"
        'approval_mode = "approve"\n'
    )


def _read_codex_provider() -> dict[str, str]:
    """Read non-secret provider settings from the current user's Codex config."""
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        raise typer.BadParameter(f"Codex config not found at {config_path}.")
    return _read_codex_provider_values(config_path.read_text(encoding="utf-8"))


def _read_codex_provider_values(config_text: str) -> dict[str, str]:
    """Extract provider settings from standard or Codex-compatible TOML."""
    try:
        config = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return _read_codex_provider_fallback(config_text)

    provider_name = config.get("model_provider")
    providers = config.get("model_providers", {})
    provider = providers.get(provider_name, {})
    return _provider_settings(provider.get("base_url"), config.get("model"))


def _read_codex_provider_fallback(config_text: str) -> dict[str, str]:
    """Read only the active provider fields when Codex TOML has extensions."""
    lines = config_text.splitlines()
    model = _find_string_setting(lines, "model")
    provider_name = _find_string_setting(lines, "model_provider")
    if not isinstance(provider_name, str):
        return _provider_settings(None, model)

    base_url: str | None = None
    active_provider = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            active_provider = stripped in {
                f"[model_providers.{provider_name}]",
                f'[model_providers."{provider_name}"]',
            }
            continue
        if active_provider:
            base_url = _find_string_setting([line], "base_url")
            if base_url is not None:
                break
    return _provider_settings(base_url, model)


def _find_string_setting(lines: list[str], key: str) -> str | None:
    """Parse a one-line TOML string assignment without reading unrelated fields."""
    for line in lines:
        stripped = line.strip()
        name, separator, raw_value = stripped.partition("=")
        if separator != "=" or name.strip() != key:
            continue
        try:
            value = tomllib.loads(f"value = {raw_value.strip()}").get("value")
        except tomllib.TOMLDecodeError:
            continue
        if isinstance(value, str):
            return value
    return None


def _provider_settings(base_url: object, model: object) -> dict[str, str]:
    """Return only recognized, non-secret provider settings."""
    values: dict[str, str] = {}
    if isinstance(base_url, str):
        values["BRAIN_LLM_BASE_URL"] = _container_gateway_url(base_url)
    if isinstance(model, str):
        values["BRAIN_LLM_MODEL"] = model
    return values


def _container_gateway_url(base_url: str) -> str:
    """Map a local Codex gateway URL to the Docker host gateway address."""
    parsed = urlsplit(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    host = "host.docker.internal"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def main() -> None:
    """Run the Brain CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        app()
    except BrokenPipeError:
        sys.exit(1)
    finally:
        flush_telemetry()


if __name__ == "__main__":
    main()

@config_app.command("install-antigravity")
def config_install_antigravity(
    mcp_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8765/mcp",
    skill_source: Annotated[Path, typer.Option()] = Path(
        "integrations/antigravity/exocortex"
    ),
) -> None:
    """Install the Exocortex skill and MCP registration for Google Antigravity."""
    skill_path = skill_source / "SKILL.md"
    if not skill_path.is_file():
        raise typer.BadParameter(f"Skill file does not exist: {skill_path}")

    home = Path.home()
    skill_target = home / ".gemini" / "config" / "skills" / "exocortex"
    skill_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_path, skill_target / "SKILL.md")

    mcp_config_path = home / ".gemini" / "config" / "mcp_config.json"
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
    _register_antigravity_mcp(mcp_config_path, mcp_url)

    typer.echo("Installed the Exocortex skill and Antigravity MCP registration.")


def _register_antigravity_mcp(config_path: Path, mcp_url: str) -> None:
    """Register the Exocortex MCP server in Antigravity mcp_config.json."""
    config: dict[str, object] = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            config = {}
    if not isinstance(config, dict):
        config = {}

    mcp_servers = config.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        config["mcpServers"] = mcp_servers

    mcp_servers["exocortex"] = {
        "serverUrl": mcp_url,
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

