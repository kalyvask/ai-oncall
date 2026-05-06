"""TelemetryStore interface. Three drivers implement it: sqlite (dev), duckdb
(single-node prod), snowflake (multi-tenant prod, stubbed in v1).

Every method takes `tenant_id` as the required first argument. There is no
"default tenant" — multi-tenancy is enforced at the query layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from ai_oncall.models import ChangeEvent, TelemetryRecord


class TelemetryStore(ABC):
    @abstractmethod
    def write_records(self, tenant_id: str, records: list[TelemetryRecord]) -> None: ...

    @abstractmethod
    def query_metric(
        self,
        tenant_id: str,
        service: str,
        metric: str,
        since: datetime,
        agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
    ) -> list[tuple[datetime, float]]: ...

    @abstractmethod
    def query_logs(
        self,
        tenant_id: str,
        service: str,
        since: datetime,
        regex: str,
        limit: int = 50,
    ) -> list[TelemetryRecord]: ...

    @abstractmethod
    def recent_deploys(
        self, tenant_id: str, service: str, since: datetime
    ) -> list[ChangeEvent]: ...

    @abstractmethod
    def query_spans(
        self, tenant_id: str, since: datetime, limit: int = 5000
    ) -> list[TelemetryRecord]: ...
