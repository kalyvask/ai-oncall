"""Prometheus HTTP API client. Implements the metrics half of the live store.

Uses the standard query_range endpoint. Service identification is by label
(default `service`, override with `AI_ONCALL_PROMETHEUS_SERVICE_LABEL`).
PromQL templates for each `agg` cover the common cases:

  avg / sum / rate: simple aggregation over the metric.
  p50 / p95 / p99 : histogram_quantile over `<metric>_bucket`. If the user's
                    metric is not a Prometheus histogram, the query returns
                    no series and the tool reports zero points — explicit,
                    not silently wrong.

Auth: pass a bearer token via `AI_ONCALL_PROMETHEUS_TOKEN`; otherwise
unauthenticated. Cap is 60 points (matches BRIEF.md §6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

POINT_LIMIT = 60
DEFAULT_STEP_SECONDS = 60
DEFAULT_RANGE = "5m"


def _promql(metric: str, service_label: str, service: str, agg: str) -> str:
    if agg in {"p50", "p95", "p99"}:
        q = {"p50": "0.50", "p95": "0.95", "p99": "0.99"}[agg]
        return (
            f"histogram_quantile({q}, "
            f'sum(rate({metric}_bucket{{{service_label}="{service}"}}[{DEFAULT_RANGE}])) '
            f"by (le))"
        )
    if agg == "rate":
        return f'rate({metric}{{{service_label}="{service}"}}[{DEFAULT_RANGE}])'
    if agg == "avg":
        return f'avg(rate({metric}{{{service_label}="{service}"}}[{DEFAULT_RANGE}]))'
    if agg == "sum":
        return f'sum(rate({metric}{{{service_label}="{service}"}}[{DEFAULT_RANGE}]))'
    raise ValueError(f"unsupported agg: {agg}")


class PrometheusClient:
    """Thin wrapper around `/api/v1/query_range`. Stateless apart from the
    underlying httpx client."""

    def __init__(
        self,
        base_url: str,
        *,
        service_label: str = "service",
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_label = service_label
        self.token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def query_metric(
        self,
        service: str,
        metric: str,
        since: datetime,
        agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
    ) -> list[tuple[datetime, float]]:
        end = datetime.now(timezone.utc)
        start = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        if start >= end:
            start = end - timedelta(minutes=5)
        params: dict[str, Any] = {
            "query": _promql(metric, self.service_label, service, agg),
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": DEFAULT_STEP_SECONDS,
        }
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        resp = self._client.get(
            f"{self.base_url}/api/v1/query_range", params=params, headers=headers
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            return []
        result = payload.get("data", {}).get("result", [])
        if not result:
            return []
        # Take the first matrix series; collapse multi-series if the user did
        # not aggregate. Adequate for v1; promote to multi-series when stage 5
        # learns to consume them.
        values = result[0].get("values", [])
        out: list[tuple[datetime, float]] = []
        for ts, value in values[:POINT_LIMIT]:
            out.append((datetime.fromtimestamp(float(ts), tz=timezone.utc), float(value)))
        return out
