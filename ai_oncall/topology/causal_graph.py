"""Causal dependency graph — the first-class abstraction the agent reasons
over when ranking hypotheses.

A ``CausalGraph`` is a thin, query-friendly wrapper around a
``TopologySnapshot``. The snapshot is the wire-format primitive (Pydantic,
serialized into ``rca_report.json``); the graph is the in-process primitive
the PRUNE step and the LLM tools use to answer two recurring questions:

  * "Can service X structurally cause an alert on service Y?"
    -> ``graph.reachable(focus=Y, candidate=X)``
  * "Which services are downstream of Y within k hops?"
    -> ``graph.downstream(Y, depth=k)``

The pruner in ``agent/causal.py`` drops hypotheses that the graph says are
unreachable from the alerting service. Naming the abstraction
"causal dependency graph" (rather than "topology") is intentional: the
graph encodes causal flow, not just service membership. An edge
``A -> B`` means "A calls B at runtime"; a fault in B can therefore surface
as latency or errors on A. The pruner uses BFS along these directed edges
to keep the 8-call investigation budget on plausible causes.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from ai_oncall.models import TopologyEdge, TopologyNode, TopologySnapshot


@dataclass(frozen=True)
class CausalGraph:
    """Directed graph of service-to-service causal edges, scoped to one tenant.

    Immutable: build it once per investigation. The pruner and any future
    graph-walking tool consume it read-only.
    """

    tenant_id: str
    captured_at: datetime
    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]

    @classmethod
    def from_snapshot(cls, snapshot: TopologySnapshot) -> CausalGraph:
        return cls(
            tenant_id=snapshot.tenant_id,
            captured_at=snapshot.captured_at,
            nodes=tuple(snapshot.nodes),
            edges=tuple(snapshot.edges),
        )

    def to_snapshot(self) -> TopologySnapshot:
        return TopologySnapshot(
            tenant_id=self.tenant_id,
            captured_at=self.captured_at,
            nodes=list(self.nodes),
            edges=list(self.edges),
        )

    def known_services(self) -> set[str]:
        return {n.service for n in self.nodes}

    def adjacency(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for edge in self.edges:
            out[edge.from_].add(edge.to)
        return dict(out)

    def reachable(self, focus: str, candidate: str) -> bool:
        """Is there a directed path ``focus -> ... -> candidate``?

        The alerting service is the ``focus``; the hypothesized root cause is
        the ``candidate``. A service is trivially reachable from itself.
        Unknown services on either side return True so a partial graph never
        prunes a hypothesis the agent might still need to chase.
        """
        if focus == candidate:
            return True
        adjacency = self.adjacency()
        return _bfs_reachable(focus, candidate, adjacency)

    def downstream(self, focus: str, depth: int = 2) -> set[str]:
        """BFS subgraph of services reachable from ``focus`` within ``depth`` hops.

        ``depth=0`` returns just ``{focus}``. The result excludes the focus
        itself only when ``focus`` does not appear among the graph's nodes.
        """
        if depth < 0:
            raise ValueError("depth must be non-negative")
        adjacency = self.adjacency()
        seen: set[str] = {focus}
        frontier: set[str] = {focus}
        for _ in range(depth):
            nxt: set[str] = set()
            for node in frontier:
                nxt |= adjacency.get(node, set()) - seen
            if not nxt:
                break
            seen |= nxt
            frontier = nxt
        return seen


def _bfs_reachable(start: str, target: str, adjacency: dict[str, set[str]]) -> bool:
    if start == target:
        return True
    queue: deque[str] = deque([start])
    seen = {start}
    while queue:
        node = queue.popleft()
        for nxt in adjacency.get(node, set()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def iter_edges(graph: CausalGraph) -> Iterator[tuple[str, str]]:
    """Convenience iterator over ``(from, to)`` pairs."""
    for edge in graph.edges:
        yield edge.from_, edge.to


__all__ = ["CausalGraph", "iter_edges"]


# --- module-level convenience constructors ----------------------------------
# These delegate to the existing builder/spans modules so callers that import
# from ``ai_oncall.topology.causal_graph`` get a single named entry point.


def from_yaml(tenant_id: str, path: Path | None = None) -> CausalGraph:
    from ai_oncall.topology.builder import load_static

    return CausalGraph.from_snapshot(load_static(tenant_id, path))


def from_spans(
    tenant_id: str,
    spans: Iterable[object],  # TelemetryRecord
    *,
    captured_at: datetime,
    window_minutes: int = 10,
) -> CausalGraph:
    from ai_oncall.topology.from_spans import from_spans as _build

    snapshot = _build(
        tenant_id,
        spans,
        captured_at=captured_at,
        window_minutes=window_minutes,  # type: ignore[arg-type]
    )
    return CausalGraph.from_snapshot(snapshot)
