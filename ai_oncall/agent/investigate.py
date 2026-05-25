"""Stage 4 — INVESTIGATE. Tool-using loop. Hard cap: 8 tool calls per incident
(BRIEF.md §6).

Given a (possibly causally pruned) hypothesis list and the 6 tools, the
agent issues queries, reads small bounded results, and refines its
hypothesis list. The loop returns a context bundle that stage 5 (SYNTHESIZE)
consumes.

This baseline implementation executes every query in order — it does NOT
yet do iterative LLM-driven refinement; that lands as `investigate_v2`
once the baseline metrics are recorded. Keeping the iteration deterministic
makes the baseline reproducible against fixtures.
"""

from __future__ import annotations

import time
from typing import Any

from ai_oncall.agent.tools import MAX_TOOL_CALLS_PER_INCIDENT, TOOL_REGISTRY
from ai_oncall.models import PlannedHypothesis, ToolCallRecord
from ai_oncall.storage.base import TelemetryStore


def investigate(
    tenant_id: str,
    hypotheses: list[PlannedHypothesis],
    store: TelemetryStore,
) -> tuple[list[ToolCallRecord], dict[str, Any]]:
    """Execute the planned queries up to MAX_TOOL_CALLS_PER_INCIDENT.

    Returns (tool_call_trace, context_bundle). The context bundle is the input
    to stage 5; the trace becomes `investigation.tool_calls` on the RcaReport.
    """
    trace: list[ToolCallRecord] = []
    bundle: dict[str, Any] = {"results": []}
    budget = MAX_TOOL_CALLS_PER_INCIDENT

    for hypothesis in hypotheses:
        for query in hypothesis.queries:
            if budget <= 0:
                bundle["budget_exhausted"] = True
                return trace, bundle
            tool = TOOL_REGISTRY[query.tool]
            t0 = time.perf_counter()
            try:
                result = tool(store, tenant_id, **query.input)
                summary = _summarize(query.tool, result)
                size = _size(result)
                error = None
            except NotImplementedError as exc:
                summary = f"tool stubbed: {exc}"
                result, size, error = None, 0, str(exc)
            duration_ms = (time.perf_counter() - t0) * 1000

            trace.append(
                ToolCallRecord(
                    tool=query.tool,
                    input=query.input,
                    result_summary=summary,
                    result_size=size,
                    duration_ms=duration_ms,
                )
            )
            bundle["results"].append(
                {
                    "tool": query.tool,
                    "input": query.input,
                    "summary": summary,
                    "result": result,
                    "error": error,
                }
            )
            budget -= 1
    return trace, bundle


def _summarize(tool: str, result: Any) -> str:
    if result is None:
        return f"{tool}: no result"
    if isinstance(result, list):
        return f"{tool}: {len(result)} item(s)"
    if isinstance(result, dict):
        for key in ("points", "lines", "nodes"):
            seq = result.get(key)
            if isinstance(seq, list):
                return f"{tool}: {len(seq)} {key}"
        return f"{tool}: {len(result)} field(s)"
    return f"{tool}: scalar"


def _size(result: Any) -> int:
    if result is None:
        return 0
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("points", "lines", "nodes"):
            seq = result.get(key)
            if isinstance(seq, list):
                return len(seq)
        return len(result)
    return 1
