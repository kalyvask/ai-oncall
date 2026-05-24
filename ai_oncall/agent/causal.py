"""Causal hypothesis elimination over the causal dependency graph.

Runs deterministically between PLAN (stage 3) and INVESTIGATE (stage 4).
Drops hypotheses whose claimed root cause cannot reach the alerting service
through the causal dependency graph; the LLM does not get to spend its 8-call
budget chasing structurally impossible causes.

Heuristics, in plain English:
  * The alerting service blames itself? Keep — services do break in place.
  * The claimed service is unknown to the graph? Keep — be generous when
    the graph is incomplete.
  * The claimed service is in the graph but unreachable from the alerting
    service along the directed call edges? Drop — there's no causal path.
  * Everything got dropped? Rescue the highest-confidence hypothesis so the
    investigator always has something to do.

Claimed services are inferred from the `service` field of each PlannedQuery's
`input` dict. The LLM already populates this on most tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_oncall.models import (
    Alert,
    InvestigationPlan,
    PlannedHypothesis,
    TopologySnapshot,
)
from ai_oncall.topology.causal_graph import CausalGraph


@dataclass(frozen=True)
class PrunedHypothesis:
    hypothesis: PlannedHypothesis
    reason: str


@dataclass(frozen=True)
class PruneResult:
    active: list[PlannedHypothesis]
    pruned: list[PrunedHypothesis]


def prune_plan(
    plan: InvestigationPlan, alert: Alert, topology: TopologySnapshot | CausalGraph
) -> PruneResult:
    graph = (
        topology
        if isinstance(topology, CausalGraph)
        else CausalGraph.from_snapshot(topology)
    )
    focus = alert.expected_focus_service or alert.service
    known = graph.known_services()

    active: list[PlannedHypothesis] = []
    pruned: list[PrunedHypothesis] = []
    for hypothesis in plan.hypotheses:
        keep, reason = _judge(hypothesis, focus, known, graph)
        if keep:
            active.append(hypothesis)
        else:
            pruned.append(PrunedHypothesis(hypothesis=hypothesis, reason=reason))

    if not active and plan.hypotheses:
        rescued = max(plan.hypotheses, key=lambda h: h.confidence)
        active = [rescued]
        pruned = [p for p in pruned if p.hypothesis is not rescued]

    return PruneResult(active=active, pruned=pruned)


def claimed_services(hypothesis: PlannedHypothesis) -> set[str]:
    services: set[str] = set()
    for query in hypothesis.queries:
        svc = query.input.get("service")
        if isinstance(svc, str) and svc:
            services.add(svc)
    return services


def _judge(
    hypothesis: PlannedHypothesis,
    focus: str,
    known: set[str],
    graph: CausalGraph,
) -> tuple[bool, str]:
    claimed = claimed_services(hypothesis)
    if not claimed:
        return True, ""

    plausible: set[str] = set()
    unreachable: set[str] = set()
    for svc in claimed:
        if svc == focus:
            plausible.add(svc)
            continue
        if svc not in known:
            plausible.add(svc)
            continue
        if graph.reachable(focus, svc):
            plausible.add(svc)
        else:
            unreachable.add(svc)

    if plausible:
        return True, ""
    sorted_unreachable = sorted(unreachable)
    return False, (
        f"no causal path from {focus} to any of {sorted_unreachable} "
        f"in the causal dependency graph"
    )
