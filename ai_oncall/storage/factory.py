"""Single entrypoint for callers that just want a configured store.

Reads `AI_ONCALL_TELEMETRY_STORE` from settings; never touches process-global
state beyond that. Snowflake raises on first use per BRIEF.md §12. The
`live` driver requires Prometheus and Loki URLs in settings.
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
    if driver == "live":
        return _make_live_store()
    raise ValueError(f"unknown telemetry store: {driver}")


def _make_live_store() -> TelemetryStore:
    from ai_oncall.storage.live import LiveStore
    from ai_oncall.storage.loki import LokiClient
    from ai_oncall.storage.prometheus import PrometheusClient
    from ai_oncall.storage.sqlite import SqliteStore

    if not settings.prometheus_url:
        raise ValueError("AI_ONCALL_TELEMETRY_STORE=live requires AI_ONCALL_PROMETHEUS_URL")
    if not settings.loki_url:
        raise ValueError("AI_ONCALL_TELEMETRY_STORE=live requires AI_ONCALL_LOKI_URL")
    prom = PrometheusClient(
        settings.prometheus_url,
        service_label=settings.prometheus_service_label,
        token=settings.prometheus_token,
    )
    loki = LokiClient(
        settings.loki_url,
        service_label=settings.loki_service_label,
        token=settings.loki_token,
    )
    deploys_delegate = SqliteStore()
    return LiveStore(prom, loki, deploys_delegate)
