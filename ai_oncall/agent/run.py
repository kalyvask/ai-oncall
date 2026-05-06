"""End-to-end orchestrator: RECEIVE -> PLAN -> PRUNE -> INVESTIGATE -> SYNTHESIZE.

This is the seam the CLI (`ai-oncall rca`) and the FastAPI agent endpoint both
call. Slack delivery (stage 6) wraps the result; the LEARN step (stage 7)
appends to learnings.jsonl and runs out-of-band.

The PRUNE step (item 3 in the roadmap) sits between PLAN and INVESTIGATE.
It reads the topology snapshot and drops hypotheses whose claimed root cause
is unreachable from the alerting service, freeing the 8-call budget for
plausible candidates only.
"""

from __future__ import annotations

from ai_oncall.agent.causal import claimed_services, prune_plan
from ai_oncall.agent.investigate import investigate
from ai_oncall.agent.plan import plan as plan_stage
from ai_oncall.agent.synthesize import synthesize
from ai_oncall.llm.client import LlmClient
from ai_oncall.models import Alert, RcaReport
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.topology.builder import build as build_topology


def run_rca(alert: Alert, store: TelemetryStore, llm: LlmClient) -> RcaReport:
    plan_obj = plan_stage(alert, llm)
    topology = build_topology(alert.tenant_id, store)
    pruned = prune_plan(plan_obj, alert, topology)
    trace, bundle = investigate(alert.tenant_id, pruned.active, store)
    if pruned.pruned:
        bundle["pruned_hypotheses"] = [
            {
                "statement": p.hypothesis.statement,
                "claimed_services": sorted(claimed_services(p.hypothesis)),
                "reason": p.reason,
            }
            for p in pruned.pruned
        ]
    return synthesize(alert, context=bundle, llm=llm, tool_calls=trace)
