"""Stage 2 — ASSEMBLE. Builds a tenant-scoped service-graph snapshot from the
static `topology.yaml` fallback. Live span-derived topology with 10-min decay
lands once OTLP ingest is wired (BRIEF.md §6 step 4 follow-up).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai_oncall.models import TopologyEdge, TopologyNode, TopologySnapshot

DEFAULT_PATH = Path("topology.yaml")


def load_static(tenant_id: str, path: Path | None = None) -> TopologySnapshot:
    target = path or DEFAULT_PATH
    if not target.exists():
        return TopologySnapshot(tenant_id=tenant_id, captured_at=datetime.now(timezone.utc), nodes=[], edges=[])
    with target.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    nodes = [TopologyNode(service=n["service"], status=n.get("status", "unknown")) for n in raw.get("nodes", [])]
    edges = [TopologyEdge.model_validate({"from": e["from"], "to": e["to"]}) for e in raw.get("edges", [])]
    return TopologySnapshot(
        tenant_id=tenant_id,
        captured_at=datetime.now(timezone.utc),
        nodes=nodes,
        edges=edges,
    )
