"""End-to-end orchestrator: RECEIVE -> PLAN -> PRUNE -> INVESTIGATE ->
SYNTHESIZE -> CORRELATE -> STAGE_ACTIONS.

This is the seam the CLI (`ai-oncall rca`) and the FastAPI agent endpoint both
call. Slack delivery (stage 6) wraps the result; the LEARN step (stage 7)
appends to learnings.jsonl and runs out-of-band.

The PRUNE step (item 3) sits between PLAN and INVESTIGATE; it drops
hypotheses whose claimed root cause is unreachable from the alerting service,
freeing the 8-call budget for plausible candidates only.

The CORRELATE step (item 4) sits after SYNTHESIZE; it attaches the most
recent deploy diff for each hypothesis's `root_cause_service` as evidence.

The STAGE_ACTIONS step (item 5) classifies each hypothesis's recommended
action into one of three trust tiers (recommend / propose / auto) for the
delivery surfaces to act on.
"""

from __future__ import annotations

from ai_oncall.agent.causal import claimed_services, prune_plan
from ai_oncall.agent.correlation import correlate_changes
from ai_oncall.agent.investigate import investigate
from ai_oncall.agent.observability import LlmTracer
from ai_oncall.agent.plan import plan as plan_stage
from ai_oncall.agent.staging import stage_actions
from ai_oncall.agent.synthesize import synthesize
from ai_oncall.llm.client import LlmClient
from ai_oncall.models import Alert, RcaReport
from ai_oncall.settings import settings
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.storage.github import GitHubClient
from ai_oncall.topology.builder import build as build_topology


def run_rca(alert: Alert, store: TelemetryStore, llm: LlmClient) -> RcaReport:
    tracer = LlmTracer()
    plan_obj = plan_stage(alert, llm, tracer=tracer)
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
    report = synthesize(alert, context=bundle, llm=llm, tool_calls=trace, tracer=tracer)
    report = correlate_changes(report, store, github=_make_github_client())
    return stage_actions(report)


def _make_github_client() -> GitHubClient | None:
    if not settings.github_repo:
        return None
    return GitHubClient(
        settings.github_repo,
        token=settings.github_token,
        api_url=settings.github_api_url,
    )
