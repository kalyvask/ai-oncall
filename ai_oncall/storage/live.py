"""Live telemetry store. Composes Prometheus (metrics) + Loki (logs) and
delegates `recent_deploys` to a local SQLite store backing the GitHub
ingest path.

This driver is selected by `AI_ONCALL_TELEMETRY_STORE=live`. It is the
opt-in path that retires the mocked tools by hitting real backends. The
deploys delegate keeps the existing change-events table working until
item 4 (GitHub HTTP connector) lands; at that point the delegate becomes
swappable.

`write_records` raises by design — live mode does not own its own
storage; OTLP ingestion does not flow through this driver.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from ai_oncall.models import ChangeEvent, TelemetryRecord
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.storage.loki import LokiClient
from ai_oncall.storage.prometheus import PrometheusClient


class LiveStore(TelemetryStore):
    def __init__(
        self,
        prometheus: PrometheusClient,
        loki: LokiClient,
        deploys_delegate: TelemetryStore,
    ) -> None:
        self._prom = prometheus
        self._loki = loki
        self._deploys = deploys_delegate

    def write_records(self, tenant_id: str, records: list[TelemetryRecord]) -> None:
        raise NotImplementedError(
            "LiveStore does not accept writes. OTLP ingest writes through the "
            "topology/decay path; metrics + logs are read live."
        )

    def query_metric(
        self,
        tenant_id: str,
        service: str,
        metric: str,
        since: datetime,
        agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
    ) -> list[tuple[datetime, float]]:
        return self._prom.query_metric(service, metric, since, agg)

    def query_logs(
        self,
        tenant_id: str,
        service: str,
        since: datetime,
        regex: str,
        limit: int = 50,
    ) -> list[TelemetryRecord]:
        return self._loki.query_logs(tenant_id, service, since, regex, limit)

    def recent_deploys(self, tenant_id: str, service: str, since: datetime) -> list[ChangeEvent]:
        return self._deploys.recent_deploys(tenant_id, service, since)

    def query_spans(
        self, tenant_id: str, since: datetime, limit: int = 5000
    ) -> list[TelemetryRecord]:
        # Live mode does not yet ship an APM/traces backend; the topology
        # builder catches NotImplementedError and falls back to topology.yaml.
        # An APM connector (Honeycomb / Datadog) plugs in here.
        raise NotImplementedError(
            "LiveStore does not have an APM/traces backend yet. "
            "Topology builder will fall back to topology.yaml."
        )
