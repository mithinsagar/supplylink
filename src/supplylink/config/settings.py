"""Application configuration.

Every knob is an environment variable with a documented default (see
``.env.example``). Nothing is read from a file at import time except the optional
local ``.env``, so the same image runs unchanged in Docker, CI, and on a laptop.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime settings resolved from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="SUPPLYLINK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://supplylink:supplylink_local_dev@localhost:5432/supplylink",
        description="SQLAlchemy URL for the canonical warehouse.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_pool_max_overflow: int = Field(default=5, ge=0, le=50)
    db_statement_timeout_ms: int = Field(default=60_000, ge=1_000)
    db_echo: bool = False

    # -- Paths -------------------------------------------------------------
    contracts_dir: Path = Field(default=REPO_ROOT / "contracts")
    data_dir: Path = Field(default=REPO_ROOT / "data")
    sql_dir: Path = Field(default=REPO_ROOT / "sql")

    # -- Pipeline behaviour ------------------------------------------------
    batch_size: int = Field(
        default=1_000,
        ge=1,
        description="Rows per executemany chunk when writing staging and canonical tables.",
    )
    max_quarantine_payload_bytes: int = Field(
        default=64_000,
        ge=1_000,
        description="Raw payloads larger than this are truncated before quarantine storage.",
    )

    # -- LLM ---------------------------------------------------------------
    llm_enabled: bool = Field(
        default=True,
        description="Master switch. When false the deterministic fallback report is used.",
    )
    llm_provider: Literal["xai", "openai", "null"] = Field(
        default="xai",
        description=(
            "Both shipped providers speak the OpenAI-compatible chat-completions "
            "shape, so they share one implementation and differ only in base URL "
            "and model. Adding a provider with a different wire format means one "
            "new LLMProvider subclass — see docs/decisions/ADR-005."
        ),
    )
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = Field(
        default=None,
        description="Overrides the provider default. Useful for proxies and local models.",
    )
    llm_model: str = "grok-4-fast"
    llm_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="Pinned to 0 by default so the same metrics yield a stable narrative.",
    )
    llm_max_output_tokens: int = Field(default=900, ge=100, le=8_000)
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_attempts: int = Field(default=3, ge=1, le=10)

    # -- Observability -----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    # -- API ---------------------------------------------------------------
    api_page_size_default: int = Field(default=50, ge=1, le=500)
    api_page_size_max: int = Field(default=500, ge=1, le=5_000)

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        """Guard against the bare ``postgresql://`` URL silently picking psycopg2."""
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @property
    def llm_configured(self) -> bool:
        """True when a live LLM call could actually be attempted."""
        return self.llm_enabled and self.llm_provider != "null" and self.llm_api_key is not None

    def masked_database_url(self) -> str:
        """The database URL with any password replaced — safe for logs and API responses."""
        url = self.database_url
        if "@" not in url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        credentials, host = rest.rsplit("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that patch the environment."""
    get_settings.cache_clear()
