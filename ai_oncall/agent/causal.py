"""Causal hypothesis elimination over the service topology.

Runs deterministically between PLAN (stage 3) and INVESTIGATE (stage 4).
Drops hypotheses whose claimed root cause cannot reach the alerting service
through the observed (or static) service graph; the LLM does not get to spend
its 8-call budget chasing structurally impossible causes.

Heuristics, in plain English:
  * The alerting service blames itself? Keep — services do break in place.
  * The claimed service is unknown to the topology? Keep — be generous when
    the graph is incomplete.
  * The claimed service is in the graph but unreachable from the alerting
    service via the call edges? Drop — there's no causal path.
  * Everything got dropped? Rescue the highest-confidence hypothesis so the
    investigator always has something to do.

Claimed services are inferred from the `service` field of each PlannedQuery's
`input` dict. The LLM already populates this on most tool calls.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from ai_oncall.models import (
    Alert,
    InvestigationPlan,
    PlannedHypothesis,
    TopologySnapshot,
)


@dataclass(frozen=True)
class PrunedHypothesis:
    hypothesis: PlannedHypothesis
    reason: str


@dataclass(frozen=True)
class PruneResult:
    active: list[PlannedHypothesis]
    pruned: list[PrunedHypothesis]


def prune_plan(
    plan: InvestigationPlan, alert: Alert, topology: TopologySnapshot
) -> PruneResult:
    focus = alert.expected_focus_service or alert.service
    known = {n.service for n in topology.nodes}
    adjacency = _adjacency(topology)

    active: list[PlannedHypothesis] = []
    pruned: list[PrunedHypothesis] = []
    for hypothesis in plan.hypotheses:
        keep, reason = _judge(hypothesis, focus, known, adjacency)
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
    adjacency: dict[str, set[str]],
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
        if _reachable(focus, svc, adjacency):
            plausible.add(svc)
        else:
            unreachable.add(svc)

    if plausible:
        return True, ""
    sorted_unreachable = sorted(unreachable)
    return False, (
        f"no causal path from {focus} to any of {sorted_unreachable} in topology"
    )


def _adjacency(topology: TopologySnapshot) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for edge in topology.edges:
        out[edge.from_].add(edge.to)
    return out


def _reachable(start: str, target: str, adjacency: dict[str, set[str]]) -> bool:
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
