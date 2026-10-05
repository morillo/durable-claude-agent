"""Typed runtime configuration.

Every knob the system reads from the environment is declared here once, with a type,
a default, and a short comment. Nothing else in the codebase calls ``os.environ``
directly; modules call :func:`get_settings` and read attributes. That gives three
things an enterprise reviewer looks for:

1. **One place to audit** what the agent can be configured to do.
2. **Fail-fast validation**: a malformed ``MAX_ROWS`` fails at startup, not mid-workflow.
3. **Testability**: tests construct ``Settings(...)`` explicitly instead of mutating env.

``pydantic-settings`` reads ``.env`` from the working directory when present, and real
environment variables always win over the file. Secrets (the API key, the capability
token) are typed as ``SecretStr`` so they never appear in ``repr()`` or logs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration, loaded from environment variables (and ``.env`` if present)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Anthropic ----------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        description="Anthropic API key. Optional at import time so unit tests and "
        "the deterministic risk classifier never need it.",
    )
    model_fast: str = Field(
        default="claude-haiku-4-5",
        description="Model for cheap steps: query planning and the final summary.",
    )
    model_strong: str = Field(
        default="claude-sonnet-5-5",
        description="Model for SQL generation with tools and for LLM-as-judge scoring.",
    )
    anthropic_fallbacks: bool = Field(
        default=True,
        description="Enable server-side refusal fallbacks (fallbacks='default') on Sonnet.",
    )

    # --- Temporal -----------------------------------------------------------
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "analyst"

    # --- MCP tool server ----------------------------------------------------
    mcp_server_url: str = "http://127.0.0.1:8765/mcp"
    run_sql_capability_token: SecretStr | None = Field(
        default=None,
        description="Bearer token the execute_sql activity presents to the MCP server. "
        "The model-facing tool session never holds it.",
    )

    # --- Lakehouse ----------------------------------------------------------
    lakehouse_path: Path = Path("./data/lakehouse")
    index_path: Path = Path("./data/index")
    governance_path: Path = Field(
        default=Path("./governance"),
        description="Folder holding data_dictionary.md and policies.md (the retrieval corpus "
        "and the machine-readable policy block).",
    )
    max_rows: int = Field(default=1000, ge=1, le=100_000)
    query_timeout_seconds: int = Field(default=30, ge=1, le=600)

    # --- Observability ------------------------------------------------------
    phoenix_collector_endpoint: str = "http://localhost:6006"
    phoenix_project_name: str = "durable-claude-agent"
    otel_enabled: bool = True

    # --- Demo hooks ---------------------------------------------------------
    demo_crash_at: str | None = Field(
        default=None,
        description="Name of the activity in which the worker deliberately kills itself "
        "(crash-recovery demo). Unset in normal operation.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so every module sees the same values. Tests call ``get_settings.cache_clear()``
    or build ``Settings`` directly.
    """
    return Settings()
