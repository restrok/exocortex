"""Tests for local configuration helpers."""

from __future__ import annotations

from exocortex.cli import (
    _mcp_server_is_registered,
    _mcp_tool_is_approved,
    _read_codex_provider_values,
)


def test_reads_gateway_from_standard_toml() -> None:
    """The active provider contributes only its endpoint and model."""
    config_text = """
model_provider = "work-gateway"
model = "gpt-5.6-luna"

[model_providers.work-gateway]
base_url = "http://gateway.test/v1"
api_key = "not-copied"
"""

    assert _read_codex_provider_values(config_text) == {
        "BRAIN_LLM_BASE_URL": "http://gateway.test/v1",
        "BRAIN_LLM_MODEL": "gpt-5.6-luna",
    }


def test_maps_local_codex_gateway_to_the_docker_host() -> None:
    """A container reaches the local Codex gateway through the host alias."""
    config_text = """
model_provider = "work-gateway"
model = "gpt-5.6-luna"

[model_providers.work-gateway]
base_url = "https://api.openai.com/v1"
"""

    assert _read_codex_provider_values(config_text)["BRAIN_LLM_BASE_URL"] == (
        "https://api.openai.com/v1"
    )


def test_reads_gateway_when_codex_toml_has_extensions() -> None:
    """Codex-only telemetry syntax does not block gateway configuration."""
    config_text = """
model_provider = "work-gateway"
model = "gpt-5.6-luna"

[otel]
exporter = {{ endpoint = "http://telemetry.test" }}

[model_providers.work-gateway]
base_url = "http://gateway.test/v1"
"""

    assert _read_codex_provider_values(config_text) == {
        "BRAIN_LLM_BASE_URL": "http://gateway.test/v1",
        "BRAIN_LLM_MODEL": "gpt-5.6-luna",
    }


def test_detects_existing_mcp_registration_without_parsing_toml() -> None:
    """Codex-specific config syntax does not prevent idempotent installation."""
    config_text = """
[otel]
exporter = {{ endpoint = "http://telemetry.test" }}

[mcp_servers.codex_work_brain]
url = "http://127.0.0.1:8765/mcp"
"""

    assert _mcp_server_is_registered(config_text)


def test_detects_the_scoped_memory_capture_approval() -> None:
    """Only the staged-memory tool is exempt from the default write prompt."""
    config_text = """
[mcp_servers.codex_work_brain]
default_tools_approval_mode = "writes"

[mcp_servers.codex_work_brain.tools.brain_remember]
approval_mode = "approve"
"""

    assert _mcp_tool_is_approved(config_text, "brain_remember")
    assert not _mcp_tool_is_approved(config_text, "brain_search")
