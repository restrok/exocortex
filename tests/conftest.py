"""Shared test helpers for Codex Brain."""

from __future__ import annotations

from pathlib import Path

from exocortex.config import Settings


def make_settings(data_dir: Path) -> Settings:
    """Create isolated test settings without reading a developer env file."""
    return Settings(
        BRAIN_DATA_DIR=str(data_dir),
        BRAIN_DEFAULT_SPACE="work",
        BRAIN_LLM_BASE_URL="http://gateway.test/v1",
        BRAIN_LLM_MODEL="gpt-5.6-luna",
        BRAIN_REFLECTION_MODEL="gpt-5.6-luna",
        BRAIN_REFLECTION_REASONING_EFFORT="high",
        BRAIN_EMBEDDING_MODEL="text-embedding-3-large",
        BRAIN_LLM_API_KEY="test-key",
        BRAIN_LLM_RESPONSE_FORMAT="json_object",
        NEO4J_URI="bolt://neo4j.test:7687",
        NEO4J_USERNAME="neo4j",
        NEO4J_PASSWORD="test-password",
        BRAIN_MCP_HOST="127.0.0.1",
        BRAIN_MCP_PORT=8765,
    )
