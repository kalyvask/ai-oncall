"""Pydantic-settings, .env-driven. One source of truth for runtime config.

Defaults track BRIEF.md §9 (claude-haiku, $0.50 ceiling, sqlite store in dev).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TelemetryDriver = Literal["sqlite", "duckdb", "snowflake", "live"]
LlmProvider = Literal["anthropic", "openai", "mock"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AI_ONCALL_", extra="ignore")

    telemetry_store: TelemetryDriver = "sqlite"
    llm_provider: LlmProvider = "mock"
    rca_model: str = "claude-haiku-4-5-20251001"
    cost_ceiling_usd: float = Field(default=0.50, ge=0)
    log_json: bool = False
    data_dir: str = "data"

    # Live store (AI_ONCALL_TELEMETRY_STORE=live). Required when live is
    # selected; ignored otherwise.
    prometheus_url: str | None = None
    prometheus_service_label: str = "service"
    prometheus_token: str | None = None
    loki_url: str | None = None
    loki_service_label: str = "service"
    loki_token: str | None = None

    # GitHub change-correlation (item 4). When unset, correlation falls back
    # to whatever patch_excerpt is already on the local ChangeEvent.
    github_token: str | None = None
    github_repo: str | None = None
    github_api_url: str = "https://api.github.com"


settings = Settings()
