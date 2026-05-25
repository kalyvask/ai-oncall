"""Derive a TopologySnapshot from a flat list of trace spans.

Pure function. No I/O, no clock — pass `captured_at` explicitly so tests are
deterministic. Edges are inferred from parent/child span relationships across
service boundaries: if span B's parent span A belongs to service S_a and B
belongs to S_b != S_a, that's a S_a -> S_b edge.

Per-edge stats:
  calls_per_min : count of B-spans for that edge / window_minutes.
  error_rate    : fraction of B-spans whose status == 'error'.
  p99_ms        : 99th percentile of B-span duration_ms (nearest-rank).

Per-node status:
  error   : at least one span on this service has status 'error' in window.
  ok      : at least one span and no errors.
  unknown : no spans seen on this service in window.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from ai_oncall.models import (
    TelemetryRecord,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
)


def from_spans(
    tenant_id: str,
    spans: Iterable[TelemetryRecord],
    *,
    captured_at: datetime,
    window_minutes: int = 10,
) -> TopologySnapshot:
    span_list = [s for s in spans if s.kind == "trace"]
    span_by_id: dict[str, TelemetryRecord] = {
        s.span_id: s for s in span_list if s.span_id is not None
    }

    services_seen: set[str] = set()
    service_status: dict[str, str] = {}
    edge_buckets: dict[tuple[str, str], list[TelemetryRecord]] = defaultdict(list)

    for span in span_list:
        services_seen.add(span.service)
        if span.status == "error":
            service_status[span.service] = "error"
        else:
            service_status.setdefault(span.service, "ok")

        if span.parent_span_id is None:
            continue
        parent = span_by_id.get(span.parent_span_id)
        if parent is None or parent.service == span.service:
            continue
        edge_buckets[(parent.service, span.service)].append(span)

    nodes = [
        TopologyNode(service=s, status=service_status.get(s, "unknown"))  # type: ignore[arg-type]
        for s in sorted(services_seen)
    ]

    edges: list[TopologyEdge] = []
    minutes = max(window_minutes, 1)
    for (src, dst), bucket in sorted(edge_buckets.items()):
        n = len(bucket)
        errors = sum(1 for s in bucket if s.status == "error")
        durations = sorted(s.duration_ms for s in bucket if s.duration_ms is not None)
        p99 = _percentile(durations, 0.99) if durations else None
        edges.append(
            TopologyEdge.model_validate(
                {
                    "from": src,
                    "to": dst,
                    "calls_per_min": n / minutes,
                    "error_rate": errors / n if n else 0.0,
                    "p99_ms": p99,
                }
            )
        )

    return TopologySnapshot(
        tenant_id=tenant_id,
        captured_at=captured_at,
        nodes=nodes,
        edges=edges,
    )


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = int(round(q * (len(sorted_values) - 1)))
    return sorted_values[idx]
