"""Configuration loading for Codex Brain."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from exocortex.models import OperationalContext


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and the env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(
        default=Path("brain"),
        validation_alias="BRAIN_DATA_DIR",
    )
    default_space: str = Field(
        default="work",
        validation_alias="BRAIN_DEFAULT_SPACE",
    )
    llm_base_url: str = Field(validation_alias="BRAIN_LLM_BASE_URL")
    llm_model: str = Field(
        default="gpt-5.6-luna",
        validation_alias="BRAIN_LLM_MODEL",
    )
    reflection_model: str = Field(
        default="gpt-5.6-luna",
        validation_alias="BRAIN_REFLECTION_MODEL",
    )
    reflection_reasoning_effort: str = Field(
        default="high",
        validation_alias="BRAIN_REFLECTION_REASONING_EFFORT",
    )
    embedding_model: str = Field(
        default="text-embedding-3-large",
        validation_alias="BRAIN_EMBEDDING_MODEL",
    )
    search_embedding_timeout_seconds: int = Field(
        default=10,
        validation_alias="BRAIN_SEARCH_EMBEDDING_TIMEOUT_SECONDS",
        ge=1,
        le=30,
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="BRAIN_LLM_API_KEY",
    )
    llm_timeout_seconds: int = Field(
        default=180,
        validation_alias="BRAIN_LLM_TIMEOUT_SECONDS",
        ge=10,
    )
    gateway_wall_timeout_seconds: int = Field(
        default=120,
        validation_alias="BRAIN_GATEWAY_WALL_TIMEOUT_SECONDS",
        ge=1,
        le=3600,
    )
    canary_timeout_seconds: int = Field(
        default=30,
        validation_alias="BRAIN_CANARY_TIMEOUT_SECONDS",
        ge=1,
        le=300,
    )
    gateway_retry_attempts: int = Field(
        default=1,
        validation_alias="BRAIN_GATEWAY_RETRY_ATTEMPTS",
        ge=0,
        le=3,
    )
    gateway_retry_backoff_seconds: float = Field(
        default=0.25,
        validation_alias="BRAIN_GATEWAY_RETRY_BACKOFF_SECONDS",
        ge=0.0,
        le=5.0,
    )
    extraction_max_chars: int = Field(
        default=16000,
        validation_alias="BRAIN_EXTRACTION_MAX_CHARS",
        ge=1000,
    )
    codex_sessions_dir: Path = Field(
        default=Path("/sources/codex"),
        validation_alias="BRAIN_CODEX_SESSIONS_DIR",
    )
    session_closed_after_seconds: int = Field(
        default=1800,
        validation_alias="BRAIN_SESSION_CLOSED_AFTER_SECONDS",
        ge=60,
    )
    ingest_max_llm_calls: int = Field(
        default=50,
        validation_alias="BRAIN_INGEST_MAX_LLM_CALLS",
        ge=0,
    )
    ingest_max_seconds: int = Field(
        default=1800,
        validation_alias="BRAIN_INGEST_MAX_SECONDS",
        ge=0,
    )
    ingest_batch_size: int = Field(
        default=5,
        validation_alias="BRAIN_INGEST_BATCH_SIZE",
        ge=1,
        le=50,
    )
    embedding_batch_size: int = Field(
        default=50,
        validation_alias="BRAIN_EMBEDDING_BATCH_SIZE",
        ge=1,
        le=250,
    )
    neo4j_upsert_batch_size: int = Field(
        default=100,
        validation_alias="BRAIN_NEO4J_UPSERT_BATCH_SIZE",
        ge=1,
        le=1000,
    )
    scheduler_backfill_enabled: bool = Field(
        default=False,
        validation_alias="BRAIN_SCHEDULER_BACKFILL_ENABLED",
    )
    scheduler_fallback_retry_enabled: bool = Field(
        default=True,
        validation_alias="BRAIN_SCHEDULER_FALLBACK_RETRY_ENABLED",
    )
    reflection_hour: int = Field(
        default=3,
        validation_alias="BRAIN_REFLECTION_HOUR",
        ge=0,
        le=23,
    )
    timezone: str = Field(
        default="America/Argentina/Buenos_Aires",
        validation_alias="BRAIN_TIMEZONE",
    )
    reflection_max_notes: int = Field(
        default=50,
        validation_alias="BRAIN_REFLECTION_MAX_NOTES",
        ge=2,
    )
    reflection_semantic_enabled: bool = Field(
        default=True,
        validation_alias="BRAIN_REFLECTION_SEMANTIC_ENABLED",
    )
    operational_context_enabled: bool = Field(
        default=False,
        validation_alias="BRAIN_OPERATIONAL_CONTEXT_ENABLED",
    )
    operational_role: str = Field(
        default="",
        validation_alias="BRAIN_OPERATIONAL_ROLE",
    )
    operational_domains: str = Field(
        default="",
        validation_alias="BRAIN_OPERATIONAL_DOMAINS",
    )
    operational_common_tasks: str = Field(
        default="",
        validation_alias="BRAIN_OPERATIONAL_COMMON_TASKS",
    )
    operational_preferred_tools: str = Field(
        default="",
        validation_alias="BRAIN_OPERATIONAL_PREFERRED_TOOLS",
    )
    operational_low_priority: str = Field(
        default="",
        validation_alias="BRAIN_OPERATIONAL_LOW_PRIORITY",
    )
    operational_risk_policy: str = Field(
        default="high_confidence_and_low_risk",
        validation_alias="BRAIN_OPERATIONAL_RISK_POLICY",
    )
    neo4j_uri: str = Field(
        default="bolt://localhost:7687",
        validation_alias="NEO4J_URI",
    )
    neo4j_username: str = Field(
        default="neo4j",
        validation_alias="NEO4J_USERNAME",
    )
    neo4j_password: SecretStr = Field(validation_alias="NEO4J_PASSWORD")
    mcp_host: str = Field(default="127.0.0.1", validation_alias="BRAIN_MCP_HOST")
    mcp_port: int = Field(default=8765, validation_alias="BRAIN_MCP_PORT")
    sync_interval_seconds: int = Field(
        default=86400,
        validation_alias="BRAIN_SYNC_INTERVAL_SECONDS",
        ge=60,
    )
    otel_enabled: bool = Field(
        default=False,
        validation_alias="BRAIN_OTEL_ENABLED",
    )
    otel_service_name: str = Field(
        default="codex-brain",
        validation_alias="BRAIN_OTEL_SERVICE_NAME",
    )
    otel_environment: str = Field(
        default="local",
        validation_alias="BRAIN_OTEL_ENVIRONMENT",
    )
    otel_exporter_endpoint: str = Field(
        default="http://localhost:4317",
        validation_alias="BRAIN_OTEL_EXPORTER_ENDPOINT",
    )
    otel_exporter_insecure: bool = Field(
        default=True,
        validation_alias="BRAIN_OTEL_EXPORTER_INSECURE",
    )
    otel_metric_export_interval_ms: int = Field(
        default=10000,
        validation_alias="BRAIN_OTEL_METRIC_EXPORT_INTERVAL_MS",
        ge=1000,
    )
    otel_span_export_delay_ms: int = Field(
        default=5000,
        validation_alias="BRAIN_OTEL_SPAN_EXPORT_DELAY_MS",
        ge=1000,
    )

    @property
    def vault_dir(self) -> Path:
        """Return the canonical Markdown vault path."""
        return self.data_dir / "Vault"

    @property
    def operational_context(self) -> OperationalContext | None:
        """Return optional recommendation context when it has been configured."""
        values = (
            self.operational_role,
            self.operational_domains,
            self.operational_common_tasks,
            self.operational_preferred_tools,
            self.operational_low_priority,
        )
        if not self.operational_context_enabled and not any(values):
            return None
        return OperationalContext(
            role=self.operational_role.strip(),
            domains=_csv_items(self.operational_domains),
            common_tasks=_csv_items(self.operational_common_tasks),
            preferred_tools=_csv_items(self.operational_preferred_tools),
            low_priority=_csv_items(self.operational_low_priority),
            risk_policy=self.operational_risk_policy.strip()
            or "high_confidence_and_low_risk",
        )

    @property
    def sanitized_dir(self) -> Path:
        """Return the sanitized source path."""
        return self.data_dir / "Sources" / "Sanitized"

    @property
    def review_dir(self) -> Path:
        """Return the review queue path."""
        return self.data_dir / "Sources" / "Review"

    @property
    def state_dir(self) -> Path:
        """Return the private runtime state path."""
        return self.data_dir / ".state"

    def prepare_directories(self) -> None:
        """Create the persistent directory structure if it does not exist."""
        for directory in (
            self.vault_dir,
            self.sanitized_dir,
            self.review_dir,
            self.state_dir,
            self.data_dir / "neo4j",
        ):
            directory.mkdir(parents=True, exist_ok=True)


def _csv_items(value: str) -> list[str]:
    """Parse a comma-separated setting into normalized non-empty items."""
    return [item.strip() for item in value.split(",") if item.strip()]
