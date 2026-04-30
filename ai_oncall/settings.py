"""Pydantic-settings, .env-driven. One source of truth for runtime config.

Defaults track BRIEF.md §9 (claude-haiku, $0.50 ceiling, sqlite store in dev).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TelemetryDriver = Literal["sqlite", "duckdb", "snowflake"]
LlmProvider = Literal["anthropic", "openai", "mock"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AI_ONCALL_", extra="ignore")

    telemetry_store: TelemetryDriver = "sqlite"
    llm_provider: LlmProvider = "mock"
    rca_model: str = "claude-haiku-4-5-20251001"
    cost_ceiling_usd: float = Field(default=0.50, ge=0)
    log_json: bool = False
    data_dir: str = "data"


settings = Settings()
