"""Slack thread follow-up: bounded Q&A over a posted RCA report.

When the engineer replies in the Slack thread (``why redis?``, ``show me
the p99``, ``any logs at the spike?``), the agent runs a tiny scoped
investigation rather than a full RCA pass:

- It loads the persisted ``RcaReport`` keyed by ``thread_ts``-style
  metadata (the parent message embeds ``report_id``).
- It picks the hypothesis the question is about (LLM classifier with the
  list of root_cause_services as the choices).
- It runs at most ``MAX_FOLLOWUP_TOOL_CALLS`` (3) of the existing six tools,
  scoped to the implicated service.
- It emits a Block Kit reply with the evidence pinned to specific tool
  results.

This is the engineer-in-the-loop reasoning module: same six tools, same
budgets, just narrower scope. It does NOT mutate the original report; it
appends a new evidence trail keyed back to that report_id for the
reasoning-trace UI to read.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ai_oncall.agent.tools import TOOL_REGISTRY
from ai_oncall.llm.client import LlmClient
from ai_oncall.models import Hypothesis, RcaReport, ToolCallRecord, ToolName
from ai_oncall.storage.base import TelemetryStore

logger = logging.getLogger(__name__)

MAX_FOLLOWUP_TOOL_CALLS = 3
FOLLOWUP_LOOKBACK_MINUTES = 30


# --- result types --------------------------------------------------------


@dataclass(frozen=True)
class ThreadAnswer:
    """The agent's reply to a thread question."""

    report_id: str
    question: str
    hypothesis_index: int  # which hypothesis from the original report this is about
    summary: str
    evidence: list[dict[str, Any]]  # tool result snippets
    tool_calls: list[ToolCallRecord]


# --- public entry point --------------------------------------------------


def answer_thread_question(
    *,
    report: RcaReport,
    question: str,
    store: TelemetryStore,
    llm: Optional[LlmClient] = None,
) -> ThreadAnswer:
    """Run a scoped follow-up investigation and return a structured answer.

    `llm` is optional: when None, falls back to deterministic intent
    extraction. Real deployments pass an LlmClient.
    """
    hypothesis_index = _pick_hypothesis(report, question, llm=llm)
    target = report.hypotheses[hypothesis_index]
    intent, suggested_tools = _classify_intent(question)
    plan = _plan_tool_calls(target, intent, suggested_tools)

    trace: list[ToolCallRecord] = []
    evidence: list[dict[str, Any]] = []
    for tool_name, tool_input in plan:
        if len(trace) >= MAX_FOLLOWUP_TOOL_CALLS:
            break
        result, record = _run_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            store=store,
            tenant_id=report.tenant_id,
        )
        trace.append(record)
        if result is not None:
            evidence.append({"tool": tool_name, "input": tool_input, "result": result})

    summary = _summarize_answer(target, intent, evidence, llm=llm)

    return ThreadAnswer(
        report_id=report.report_id,
        question=question,
        hypothesis_index=hypothesis_index,
        summary=summary,
        evidence=evidence,
        tool_calls=trace,
    )


# --- intent classification ----------------------------------------------


_INTENT_PATTERNS: tuple[tuple[str, str, tuple[ToolName, ...]], ...] = (
    # (regex, intent_label, tool list to consider). Patterns are evaluated in
    # order; show_metric must come before show_logs so "error rate" goes to
    # metrics, not logs.
    (r"\b(metric|p\d{2,3}|latency|error\s+rate|cpu|memory|disk)\b", "show_metric", ("query_metrics",)),
    (r"\b(logs?|errors?|exceptions?|stacktraces?)\b", "show_logs", ("query_logs",)),
    (r"\b(deploys?|rollouts?|merges?|pr|commits?)\b", "show_deploys", ("get_recent_deploys",)),
    (r"\b(runbook|playbook|sop)\b", "show_runbook", ("get_runbook",)),
    (r"\b(graph|topology|dependency|callers?|callees?)\b", "show_topology", ("get_topology",)),
    (r"\b(history|past|before|previous|seen\s+this)\b", "show_history", ("get_past_incidents",)),
    (r"\b(why|how\s+come|what\s+made)\b", "explain", ("query_logs", "query_metrics")),
)


def _classify_intent(question: str) -> tuple[str, tuple[ToolName, ...]]:
    """Pick the most specific intent for the question.

    Returns (intent_label, tools_to_try). Default falls back to logs +
    metrics, the two most generally informative tools.
    """
    q = question.lower()
    for pattern, label, tools in _INTENT_PATTERNS:
        if re.search(pattern, q):
            return label, tools
    return "explain", ("query_logs", "query_metrics")


# --- hypothesis picking --------------------------------------------------


def _pick_hypothesis(
    report: RcaReport, question: str, *, llm: Optional[LlmClient]
) -> int:
    """Decide which of the report's hypotheses the question is about.

    Cheap heuristic first: if the question mentions a service name from any
    hypothesis, pick that one. Otherwise use the LLM (when available) to
    classify; otherwise default to the top hypothesis.
    """
    q = question.lower()
    # 1. Direct service-name match.
    for idx, h in enumerate(report.hypotheses):
        if h.root_cause_service.lower() in q:
            return idx

    # 2. LLM short-form classifier (only when llm provided).
    if llm is not None:
        choices = ", ".join(
            f"{i}: {h.root_cause_service}" for i, h in enumerate(report.hypotheses)
        )
        prompt = (
            f"User question: {question}\n"
            f"Choose which hypothesis the question is about. Reply with only the integer.\n"
            f"Choices: {choices}"
        )
        try:
            response = llm.generate(prompt, max_tokens=8)
            text = response.get("text", "") if isinstance(response, dict) else str(response)
            match = re.search(r"\d+", text)
            if match:
                idx = int(match.group(0))
                if 0 <= idx < len(report.hypotheses):
                    return idx
        except Exception:
            logger.exception("thread_qa_llm_classify_failed")

    # 3. Default: top hypothesis.
    return 0


# --- tool planning -------------------------------------------------------


def _plan_tool_calls(
    target: Hypothesis,
    intent: str,
    tool_names: tuple[ToolName, ...],
) -> list[tuple[ToolName, dict[str, Any]]]:
    """Build a tiny tool plan for the chosen intent + hypothesis.

    Each tool gets sensible defaults scoped to the implicated service and
    a 30-minute lookback window. The agent can run at most 3, so we return
    up to 3 candidates ordered by how specific the intent is.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=FOLLOWUP_LOOKBACK_MINUTES)).isoformat()
    service = target.root_cause_service
    plan: list[tuple[ToolName, dict[str, Any]]] = []

    for tool_name in tool_names:
        if tool_name == "query_metrics":
            # Default to p99 latency — the most common follow-up signal.
            plan.append(
                ("query_metrics", {"service": service, "metric": "latency_ms", "since": since, "agg": "p99"})
            )
        elif tool_name == "query_logs":
            plan.append(
                ("query_logs", {"service": service, "since": since, "regex": "(?i)error|warn|fail|timeout", "limit": 20})
            )
        elif tool_name == "get_recent_deploys":
            plan.append(
                ("get_recent_deploys", {"service": service, "since": since})
            )
        elif tool_name == "get_runbook":
            plan.append(("get_runbook", {"service": service}))
        elif tool_name == "get_topology":
            plan.append(("get_topology", {"service": service, "depth": 2}))
        elif tool_name == "get_past_incidents":
            plan.append(("get_past_incidents", {"service": service, "k": 3}))
        if len(plan) >= MAX_FOLLOWUP_TOOL_CALLS:
            break
    return plan


# --- tool execution ------------------------------------------------------


def _run_tool(
    *,
    tool_name: ToolName,
    tool_input: dict[str, Any],
    store: TelemetryStore,
    tenant_id: str,
) -> tuple[Optional[Any], ToolCallRecord]:
    """Execute one tool. Returns (result, record). Best-effort — exceptions
    are caught and surfaced as `result_summary`."""
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return None, ToolCallRecord(
            tool=tool_name,
            input=tool_input,
            result_summary=f"unknown tool: {tool_name}",
            result_size=0,
            duration_ms=0,
        )
    started = datetime.now(timezone.utc)
    try:
        result = fn(store, tenant_id, **tool_input)
    except Exception as e:
        logger.exception("thread_qa_tool_failed", extra={"tool": tool_name})
        return None, ToolCallRecord(
            tool=tool_name,
            input=tool_input,
            result_summary=f"error: {e}",
            result_size=0,
            duration_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
        )
    duration = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    summary = _summarize_tool_result(tool_name, result)
    size = _result_size(result)
    return result, ToolCallRecord(
        tool=tool_name,
        input=tool_input,
        result_summary=summary,
        result_size=size,
        duration_ms=duration,
    )


def _summarize_tool_result(tool_name: ToolName, result: Any) -> str:
    if tool_name == "query_metrics" and isinstance(result, dict):
        points = result.get("points", []) if isinstance(result.get("points"), list) else []
        return f"{len(points)} metric points"
    if tool_name == "query_logs" and isinstance(result, dict):
        lines = result.get("lines", []) if isinstance(result.get("lines"), list) else []
        return f"{len(lines)} log lines"
    if tool_name == "get_recent_deploys" and isinstance(result, list):
        return f"{len(result)} recent deploys"
    if tool_name == "get_runbook":
        return "runbook present" if result else "no runbook"
    if tool_name == "get_topology" and isinstance(result, dict):
        return f"{len(result.get('nodes', []))} nodes, {len(result.get('edges', []))} edges"
    if tool_name == "get_past_incidents" and isinstance(result, list):
        return f"{len(result)} past incidents"
    return "ok"


def _result_size(result: Any) -> int:
    try:
        return len(json.dumps(result, default=str))
    except Exception:
        return 0


# --- summarization -------------------------------------------------------


def _summarize_answer(
    target: Hypothesis,
    intent: str,
    evidence: list[dict[str, Any]],
    *,
    llm: Optional[LlmClient],
) -> str:
    """Compose a 1–3 sentence answer.

    With an LLM, we ask it to narrate the evidence; without, we fall back
    to a deterministic template tied to the intent.
    """
    if llm is not None and evidence:
        snippets = json.dumps(evidence, default=str)[:3000]
        prompt = (
            f"You are answering a follow-up question in a Slack thread about "
            f"the hypothesis: '{target.reasoning}'. The user's intent is "
            f"'{intent}'. The tools returned this evidence (truncated):\n\n"
            f"{snippets}\n\n"
            f"Reply with 1-3 sentences. Be specific. Cite numbers from the "
            f"evidence. Do not speculate beyond what the evidence shows."
        )
        try:
            response = llm.generate(prompt, max_tokens=200)
            text = response.get("text", "") if isinstance(response, dict) else str(response)
            if text.strip():
                return text.strip()
        except Exception:
            logger.exception("thread_qa_summarize_failed")

    # Deterministic fallback.
    if not evidence:
        return (
            f"No new evidence available for `{target.root_cause_service}` "
            f"in the last {FOLLOWUP_LOOKBACK_MINUTES} minutes."
        )
    parts = [f"Looked at `{target.root_cause_service}`:"]
    for e in evidence:
        tool = e["tool"]
        result = e.get("result")
        if tool == "query_metrics" and isinstance(result, dict):
            points = result.get("points", [])
            if points:
                latest = points[-1]
                parts.append(
                    f"latest {result.get('metric','metric')}={latest.get('v')} at {latest.get('t')}."
                )
        elif tool == "query_logs" and isinstance(result, dict):
            lines = result.get("lines", [])
            if lines:
                parts.append(f"{len(lines)} matching log lines; first severity={lines[0].get('severity')}.")
        elif tool == "get_recent_deploys" and isinstance(result, list):
            if result:
                parts.append(f"{len(result)} deploys, most recent at {result[0].get('timestamp')}.")
        elif tool == "get_runbook":
            parts.append("runbook attached." if result else "no runbook for this service.")
        elif tool == "get_topology" and isinstance(result, dict):
            parts.append(f"topology: {len(result.get('nodes',[]))} nodes within depth 2.")
        elif tool == "get_past_incidents" and isinstance(result, list):
            parts.append(f"{len(result)} past incidents on this service.")
    return " ".join(parts)


# --- Slack reply rendering ----------------------------------------------


def render_answer_blocks(answer: ThreadAnswer) -> list[dict[str, Any]]:
    """Block Kit blocks for posting the answer back into the thread."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Q:* {answer.question}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": answer.summary},
        },
    ]
    if answer.tool_calls:
        used = ", ".join(f"`{tc.tool}`" for tc in answer.tool_calls)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"tools used: {used}"},
                    {"type": "mrkdwn", "text": f"hypothesis #{answer.hypothesis_index + 1}"},
                ],
            }
        )
    return blocks
