"""Stage 2 — ASSEMBLE. Builds a tenant-scoped service-graph snapshot.

Two sources, in priority order:
  1. Live spans queried from the TelemetryStore (last `window_minutes`).
  2. The static `topology.yaml` fallback.

`build()` is the entry point; it tries live spans first and silently falls
back to yaml if the store has no spans, or raises (e.g. live mode with no
APM backend wired). `load_static` and `load_from_spans` are exposed for
callers that want one source explicitly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from ai_oncall.models import TopologyEdge, TopologyNode, TopologySnapshot
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.topology.from_spans import from_spans

DEFAULT_PATH = Path("topology.yaml")
DEFAULT_WINDOW_MINUTES = 10


def load_static(tenant_id: str, path: Path | None = None) -> TopologySnapshot:
    target = path or DEFAULT_PATH
    if not target.exists():
        return TopologySnapshot(
            tenant_id=tenant_id, captured_at=datetime.now(timezone.utc), nodes=[], edges=[]
        )
    with target.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    nodes = [
        TopologyNode(service=n["service"], status=n.get("status", "unknown"))
        for n in raw.get("nodes", [])
    ]
    edges = [
        TopologyEdge.model_validate({"from": e["from"], "to": e["to"]})
        for e in raw.get("edges", [])
    ]
    return TopologySnapshot(
        tenant_id=tenant_id,
        captured_at=datetime.now(timezone.utc),
        nodes=nodes,
        edges=edges,
    )


def load_from_spans(
    tenant_id: str,
    store: TelemetryStore,
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    now: datetime | None = None,
) -> TopologySnapshot:
    captured_at = now or datetime.now(timezone.utc)
    since = captured_at - timedelta(minutes=window_minutes)
    spans = store.query_spans(tenant_id, since)
    return from_spans(tenant_id, spans, captured_at=captured_at, window_minutes=window_minutes)


def build(
    tenant_id: str,
    store: TelemetryStore | None = None,
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    static_path: Path | None = None,
    now: datetime | None = None,
) -> TopologySnapshot:
    """Live spans first; fall back to topology.yaml when none are available."""
    if store is not None:
        try:
            live = load_from_spans(tenant_id, store, window_minutes=window_minutes, now=now)
        except NotImplementedError:
            live = None
        if live is not None and live.nodes:
            return live
    return load_static(tenant_id, static_path)
