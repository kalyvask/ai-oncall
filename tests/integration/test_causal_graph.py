"""CausalGraph — the first-class graph abstraction the pruner walks."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_oncall.models import TopologyEdge, TopologyNode, TopologySnapshot
from ai_oncall.topology.causal_graph import CausalGraph


def _snapshot(edges: list[tuple[str, str]], nodes: list[str] | None = None) -> TopologySnapshot:
    return TopologySnapshot(
        tenant_id="demo",
        captured_at=datetime.now(timezone.utc),
        nodes=[TopologyNode(service=s, status="ok") for s in (nodes or _services_from(edges))],
        edges=[TopologyEdge.model_validate({"from": a, "to": b}) for a, b in edges],
    )


def _services_from(edges: list[tuple[str, str]]) -> list[str]:
    seen: set[str] = set()
    for a, b in edges:
        seen.add(a)
        seen.add(b)
    return sorted(seen)


def test_reachable_follows_directed_edges() -> None:
    graph = CausalGraph.from_snapshot(_snapshot([("checkout", "payment"), ("payment", "stripe")]))
    assert graph.reachable("checkout", "stripe")
    # Edges are directed: stripe cannot reach checkout.
    assert not graph.reachable("stripe", "checkout")


def test_reachable_self_is_true() -> None:
    graph = CausalGraph.from_snapshot(_snapshot([("checkout", "payment")]))
    assert graph.reachable("checkout", "checkout")


def test_downstream_within_depth() -> None:
    graph = CausalGraph.from_snapshot(
        _snapshot(
            [
                ("checkout", "payment"),
                ("payment", "stripe"),
                ("stripe", "card_network"),
            ]
        )
    )
    assert graph.downstream("checkout", depth=1) == {"checkout", "payment"}
    assert graph.downstream("checkout", depth=2) == {"checkout", "payment", "stripe"}
    assert graph.downstream("checkout", depth=10) == {
        "checkout",
        "payment",
        "stripe",
        "card_network",
    }


def test_downstream_rejects_negative_depth() -> None:
    graph = CausalGraph.from_snapshot(_snapshot([("a", "b")]))
    with pytest.raises(ValueError):
        graph.downstream("a", depth=-1)


def test_known_services_excludes_edge_only_nodes() -> None:
    snap = TopologySnapshot(
        tenant_id="demo",
        captured_at=datetime.now(timezone.utc),
        nodes=[TopologyNode(service="checkout", status="ok")],
        edges=[TopologyEdge.model_validate({"from": "checkout", "to": "payment"})],
    )
    graph = CausalGraph.from_snapshot(snap)
    assert graph.known_services() == {"checkout"}


def test_round_trip_snapshot() -> None:
    snap = _snapshot([("checkout", "payment")])
    graph = CausalGraph.from_snapshot(snap)
    rebuilt = graph.to_snapshot()
    assert rebuilt.tenant_id == snap.tenant_id
    assert {n.service for n in rebuilt.nodes} == {n.service for n in snap.nodes}
    assert [(e.from_, e.to) for e in rebuilt.edges] == [(e.from_, e.to) for e in snap.edges]
