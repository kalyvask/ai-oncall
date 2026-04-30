"""End-to-end orchestrator: RECEIVE -> PLAN -> INVESTIGATE -> SYNTHESIZE.

This is the seam the CLI (`ai-oncall rca`) and the FastAPI agent endpoint both
call. Slack delivery (stage 6) wraps the result; the LEARN step (stage 7)
appends to learnings.jsonl and runs out-of-band.
"""

from __future__ import annotations

from ai_oncall.agent.investigate import investigate
from ai_oncall.agent.plan import plan as plan_stage
from ai_oncall.agent.synthesize import synthesize
from ai_oncall.llm.client import LlmClient
from ai_oncall.models import Alert, RcaReport
from ai_oncall.storage.base import TelemetryStore


def run_rca(alert: Alert, store: TelemetryStore, llm: LlmClient) -> RcaReport:
    plan_obj = plan_stage(alert, llm)
    trace, bundle = investigate(plan_obj, store)
    return synthesize(alert, context=bundle, llm=llm, tool_calls=trace)
