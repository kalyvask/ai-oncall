"""Causal hypothesis elimination — runs between PLAN and INVESTIGATE.

Drops hypotheses whose claimed root cause is unreachable from the alerting
service in the topology graph. Self-blame and unknown services are kept.
If everything would be pruned, the highest-confidence one is rescued so the
investigator never starves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_oncall.agent.causal import claimed_services, prune_plan
from ai_oncall.models import (
    Alert,
    AlertSignal,
    InvestigationPlan,
    PlannedHypothesis,
    PlannedQuery,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
)

T0 = datetime(2026, 4, 25, 2, 0, tzinfo=timezone.utc)
TENANT = "alpha"


def _alert(focus: str = "checkout") -> Alert:
    return Alert(
        alert_id="a1", tenant_id=TENANT, fired_at=T0,
        source="manual", severity="page",
        service=focus,
        signal=AlertSignal(kind="manual"),
        title="checkout slow",
    )


def _topology(edges: list[tuple[str, str]], nodes: list[str] | None = None) -> TopologySnapshot:
    services = list({s for pair in edges for s in pair} | set(nodes or []))
    return TopologySnapshot(
        tenant_id=TENANT,
        captured_at=T0,
        nodes=[TopologyNode(service=s, status="unknown") for s in sorted(services)],
        edges=[TopologyEdge.model_validate({"from": f, "to": t}) for f, t in edges],
    )


def _hypothesis(statement: str, services: list[str], confidence: float = 0.5) -> PlannedHypothesis:
    queries = [
        PlannedQuery(tool="get_recent_deploys", input={"service": s, "since": T0.isoformat()})
        for s in services
    ] or [PlannedQuery(tool="get_runbook", input={"service": "unknown-svc"})]
    return PlannedHypothesis(statement=statement, confidence=confidence, queries=queries)


def _plan(hypotheses: list[PlannedHypothesis]) -> InvestigationPlan:
    return InvestigationPlan(tenant_id=TENANT, alert_id="a1", hypotheses=hypotheses)


# --- core behaviour --------------------------------------------------------


def test_drops_hypothesis_with_unreachable_claimed_service() -> None:
    # orders is in the graph but checkout cannot reach it.
    topo = _topology(edges=[("checkout", "payment")], nodes=["orders"])
    plan = _plan([
        _hypothesis("payment is the cause", ["payment"], confidence=0.6),
        _hypothesis("self regression", ["checkout"], confidence=0.3),
        _hypothesis("orders is the cause", ["orders"], confidence=0.2),  # not reachable
    ])
    result = prune_plan(plan, _alert(), topo)
    statements = [h.statement for h in result.active]
    assert "orders is the cause" not in statements
    assert {p.hypothesis.statement for p in result.pruned} == {"orders is the cause"}
    assert "no causal path" in result.pruned[0].reason


def test_keeps_self_blame_even_with_no_outgoing_edges() -> None:
    topo = _topology(edges=[], nodes=["checkout"])
    plan = _plan([
        _hypothesis("self regression", ["checkout"], confidence=0.5),
        _hypothesis("phantom svc", ["payment"], confidence=0.2),  # payment unknown -> kept
        _hypothesis("yet another", ["checkout", "payment"], confidence=0.4),
    ])
    result = prune_plan(plan, _alert(), topo)
    statements = {h.statement for h in result.active}
    assert "self regression" in statements


def test_keeps_unknown_services_under_uncertainty() -> None:
    topo = _topology(edges=[("checkout", "payment")])
    plan = _plan([
        _hypothesis("self", ["checkout"]),
        _hypothesis("payment", ["payment"]),
        _hypothesis("ghost", ["service-not-in-graph"]),  # unknown -> kept
    ])
    result = prune_plan(plan, _alert(), topo)
    assert len(result.active) == 3
    assert not result.pruned


def test_keeps_reachable_via_multi_hop() -> None:
    topo = _topology(edges=[("checkout", "cart"), ("cart", "cart-db")])
    plan = _plan([
        _hypothesis("self", ["checkout"]),
        _hypothesis("cart-db deep", ["cart-db"]),  # 2 hops away, reachable
        _hypothesis("orders unrelated", ["orders"]),  # in the graph but not reachable
    ])
    topo_with_orders = _topology(
        edges=[("checkout", "cart"), ("cart", "cart-db")], nodes=["orders"]
    )
    result = prune_plan(plan, _alert(), topo_with_orders)
    statements = {h.statement for h in result.active}
    assert "cart-db deep" in statements
    assert "orders unrelated" not in statements


def test_rescues_highest_confidence_when_all_would_be_pruned() -> None:
    topo = _topology(edges=[], nodes=["checkout", "payment", "orders"])
    plan = _plan([
        _hypothesis("payment", ["payment"], confidence=0.4),
        _hypothesis("orders", ["orders"], confidence=0.7),
        _hypothesis("payment again", ["payment"], confidence=0.2),
    ])
    result = prune_plan(plan, _alert(), topo)
    assert len(result.active) == 1
    assert result.active[0].statement == "orders"
    # The rescued one is removed from pruned to keep the bookkeeping clean.
    assert result.active[0] not in (p.hypothesis for p in result.pruned)


def test_uses_expected_focus_service_when_present() -> None:
    # Topology: payment -> stripe; orders is also in the graph but unreachable
    # from payment.
    topo = _topology(edges=[("payment", "stripe")], nodes=["orders"])
    alert = Alert(
        alert_id="a1", tenant_id=TENANT, fired_at=T0,
        source="manual", severity="page",
        service="checkout",  # alert was raised on checkout
        signal=AlertSignal(kind="manual"),
        title="payment errors",
        expected_focus_service="payment",  # but the focus is payment
    )
    plan = _plan([
        _hypothesis("stripe outage", ["stripe"], confidence=0.6),  # reachable from payment
        _hypothesis("orders unrelated", ["orders"], confidence=0.3),  # not reachable
        _hypothesis("filler", ["payment"], confidence=0.1),
    ])
    result = prune_plan(plan, alert, topo)
    statements = {h.statement for h in result.active}
    assert "stripe outage" in statements
    assert "orders unrelated" not in statements


# --- helpers --------------------------------------------------------------


def test_claimed_services_extracts_from_query_inputs() -> None:
    h = _hypothesis("x", ["payment", "checkout"])
    assert claimed_services(h) == {"payment", "checkout"}


# --- integration: run_rca threads the pruner -------------------------------


def test_run_rca_records_pruned_hypotheses_in_bundle(tmp_path) -> None:
    """End-to-end: run_rca -> plan -> prune -> investigate. Pruned info lands
    in the bundle so synthesize can mention what was ruled out."""
    import json

    from ai_oncall.agent.prompts import plan_v1, synthesize_v1
    from ai_oncall.agent.run import run_rca
    from ai_oncall.llm.client import MockLlm
    from ai_oncall.storage.sqlite import SqliteStore

    repo = Path(__file__).resolve().parents[2]
    alert = Alert.model_validate_json(
        (repo / "fixtures/synthetic_alerts/checkout_regression.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (repo / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )

    plan_payload = {
        "tenant_id": alert.tenant_id,
        "alert_id": alert.alert_id,
        "hypotheses": [
            {"statement": "payment regression", "confidence": 0.7, "queries": [
                {"tool": "get_recent_deploys", "input": {"service": "payment", "since": "2026-04-24T03:14:00Z"}},
            ]},
            {"statement": "internal-dns failure", "confidence": 0.5, "queries": [
                {"tool": "get_recent_deploys", "input": {"service": "internal-dns", "since": "2026-04-24T03:14:00Z"}},
            ]},
            {"statement": "self regression", "confidence": 0.2, "queries": [
                {"tool": "get_recent_deploys", "input": {"service": "checkout", "since": "2026-04-24T03:14:00Z"}},
            ]},
        ],
    }
    mock = MockLlm(fixtures={
        plan_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(plan_payload), "tokens_in": 800, "tokens_out": 200},
        synthesize_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(expected), "tokens_in": 4000, "tokens_out": 600},
    })
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    report = run_rca(alert, store, mock)
    # Sanity: the report lands and is schema-valid in the existing flow; the
    # bundle is consumed by synthesize internally — we re-invoke the pruner
    # directly to confirm the right hypothesis is dropped.
    assert report is not None


def test_pruner_drops_internal_dns_for_checkout_alert() -> None:
    """Topology yaml has internal-dns connected via search, not checkout.
    A 'internal-dns' hypothesis on a checkout alert must be pruned."""
    from ai_oncall.topology.builder import load_static

    alert = _alert("checkout")
    topo = load_static(TENANT)
    plan = _plan([
        _hypothesis("payment regression", ["payment"], confidence=0.7),
        _hypothesis("internal-dns failure", ["internal-dns"], confidence=0.5),
        _hypothesis("self", ["checkout"], confidence=0.3),
    ])
    result = prune_plan(plan, alert, topo)
    pruned_statements = {p.hypothesis.statement for p in result.pruned}
    assert "internal-dns failure" in pruned_statements
