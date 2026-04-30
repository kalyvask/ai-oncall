"""Single entrypoint for callers that just want a configured store.

Reads `AI_ONCALL_TELEMETRY_STORE` from settings; never touches process-global
state beyond that. Snowflake raises on first use per BRIEF.md §12.
"""

from __future__ import annotations

from ai_oncall.settings import settings
from ai_oncall.storage.base import TelemetryStore


def make_store() -> TelemetryStore:
    driver = settings.telemetry_store
    if driver == "sqlite":
        from ai_oncall.storage.sqlite import SqliteStore

        return SqliteStore()
    if driver == "duckdb":
        from ai_oncall.storage.duckdb import DuckDbStore

        return DuckDbStore()
    if driver == "snowflake":
        from ai_oncall.storage.snowflake import SnowflakeStore

        return SnowflakeStore()
    raise ValueError(f"unknown telemetry store: {driver}")
