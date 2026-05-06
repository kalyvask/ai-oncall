"""The 6 tools the LLM gets. BRIEF.md §6.

Each tool has a strict input schema (defined inline below), a max result size,
and is deterministic given the same inputs. The LLM never receives raw rows;
results are summarized into small structured payloads.

Tools delegate to the TelemetryStore (multi-tenant), the topology builder, and
runbook lookup. None of them call out to the LLM.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from ai_oncall.models import (
    ChangeEvent,
    TelemetryRecord,
    TopologySnapshot,
)
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.topology.builder import build as build_topology


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- tool implementations ---------------------------------------------------


def query_metrics(
    store: TelemetryStore,
    tenant_id: str,
    *,
    service: str,
    metric: str,
    since: str,
    agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"],
) -> dict[str, Any]:
    points = store.query_metric(tenant_id, service, metric, _parse_iso(since), agg)[:60]
    return {"service": service, "metric": metric, "agg": agg, "points": [
        {"t": t.isoformat(), "v": v} for t, v in points
    ]}


def query_logs(
    store: TelemetryStore,
    tenant_id: str,
    *,
    service: str,
    since: str,
    regex: str,
    limit: int = 50,
) -> dict[str, Any]:
    capped = min(limit, 50)
    rows: list[TelemetryRecord] = store.query_logs(tenant_id, service, _parse_iso(since), regex, capped)
    return {"service": service, "lines": [
        {"t": r.timestamp.isoformat(), "severity": r.severity, "body": r.body} for r in rows[:capped]
    ]}


def get_recent_deploys(
    store: TelemetryStore,
    tenant_id: str,
    *,
    service: str,
    since: str,
) -> list[dict[str, Any]]:
    events: list[ChangeEvent] = store.recent_deploys(tenant_id, service, _parse_iso(since))[:25]
    return [{
        "event_id": e.event_id, "kind": e.kind, "timestamp": e.timestamp.isoformat(),
        "actor": e.actor, "title": e.title, "sha": e.sha,
    } for e in events]


def get_runbook(_store: TelemetryStore, _tenant_id: str, *, service: str) -> str | None:
    path = Path("runbooks") / f"{service}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def get_topology(
    store: TelemetryStore,
    tenant_id: str,
    *,
    service: str,
    depth: int = 2,
) -> dict[str, Any]:
    snap: TopologySnapshot = build_topology(tenant_id, store)
    nodes = {n.service: n for n in snap.nodes}
    if service not in nodes:
        return {"service": service, "nodes": [], "edges": []}
    visited = {service}
    frontier = {service}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for edge in snap.edges:
            if edge.from_ in frontier and edge.to not in visited:
                next_frontier.add(edge.to)
            if edge.to in frontier and edge.from_ not in visited:
                next_frontier.add(edge.from_)
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    return {
        "service": service,
        "nodes": [{"service": s, "status": nodes[s].status if s in nodes else "unknown"} for s in visited],
        "edges": [{"from": e.from_, "to": e.to} for e in snap.edges if e.from_ in visited and e.to in visited],
    }


def get_past_incidents(
    _store: TelemetryStore, _tenant_id: str, *, service: str, k: int = 3
) -> list[dict[str, Any]]:
    """k-NN over learnings.jsonl. Stub returns []; lands fully in BRIEF.md step 7."""
    _ = service, k
    return []


# --- registry ---------------------------------------------------------------


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "query_metrics": query_metrics,
    "query_logs": query_logs,
    "get_recent_deploys": get_recent_deploys,
    "get_runbook": get_runbook,
    "get_topology": get_topology,
    "get_past_incidents": get_past_incidents,
}

MAX_TOOL_CALLS_PER_INCIDENT = 8
