"""Snowflake driver. Stubbed until a real customer needs it (BRIEF.md §11 step 10).

Every method here raises NotImplementedError with a clear pointer. Do not stub
with silent no-ops — silent stubs ship and pretend to work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from ai_oncall.models import ChangeEvent, TelemetryRecord
from ai_oncall.storage.base import TelemetryStore

_MSG = (
    "Snowflake driver is stubbed in v1 (BRIEF.md §12). "
    "Set AI_ONCALL_TELEMETRY_STORE=sqlite or duckdb. "
    "Implement only when a real customer requires it."
)


class SnowflakeStore(TelemetryStore):
    def write_records(self, tenant_id: str, records: list[TelemetryRecord]) -> None:
        raise NotImplementedError(_MSG)

    def query_metric(
        self,
        tenant_id: str,
        service: str,
        metric: str,
        since: datetime,
        agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
    ) -> list[tuple[datetime, float]]:
        raise NotImplementedError(_MSG)

    def query_logs(
        self,
        tenant_id: str,
        service: str,
        since: datetime,
        regex: str,
        limit: int = 50,
    ) -> list[TelemetryRecord]:
        raise NotImplementedError(_MSG)

    def recent_deploys(
        self, tenant_id: str, service: str, since: datetime
    ) -> list[ChangeEvent]:
        raise NotImplementedError(_MSG)

    def query_spans(
        self, tenant_id: str, since: datetime, limit: int = 5000
    ) -> list[TelemetryRecord]:
        raise NotImplementedError(_MSG)
