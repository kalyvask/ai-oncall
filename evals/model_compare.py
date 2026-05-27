"""Live-prediction model comparison for the synthetic eval track.

The default eval mode is replay: predicted == expected, all metrics 1.0,
the path is exercised for regression detection. That is not useful when
the question is "how good is Haiku vs Sonnet vs Opus at producing the RCA
write-up in the first place?"

This module adds a single-shot live predictor: for each synthetic case,
feed the model the alert and a terse summary of the expected investigation
trail, ask it for a top-hypothesis RCA, parse the JSON, and score the
prediction against the expected fixture. Pairwise scores plus per-model
cost are emitted as a Markdown table that drops straight into the README.

This evaluates RCA *synthesis quality* under perfect context. It is not the
full agent loop (no tool calls, no plan/prune/correlate) — that's deliberate
so the eval is fast and apples-to-apples across models. Hooking the whole
``run_rca`` path through ``_live_predict`` would change the metric to
"investigation skill including tool use" — a different (also useful) test.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from ai_oncall.llm.client import AnthropicLlm, LlmClient, MockLlm
from ai_oncall.llm.registry import CATALOG, estimate_cost
from ai_oncall.models import (
    Alert,
    EvidenceItem,
    Hypothesis,
    ModelRef,
    RcaReport,
)
from evals.scoring import score_all

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are a site-reliability incident analyst. Given an alert and a brief "
    "investigation summary, produce a single ranked root-cause hypothesis. "
    "Be specific about the implicated service. Cite the strongest signal in your "
    "reasoning. Keep the reasoning under three sentences."
)

_USER_TEMPLATE = """ALERT (JSON):
{alert_json}

INVESTIGATION SUMMARY (tool calls + headline findings):
{investigation_summary}

KNOWN SERVICES IN THE TOPOLOGY:
{topology_hint}

Output JSON ONLY with this exact shape:
{{
  "root_cause_service": "<service name>",
  "confidence": <float 0..1>,
  "reasoning": "<1-3 sentences>",
  "recommended_action": "<imperative phrase>",
  "should_escalate": <true|false>
}}"""


@dataclass
class ModelCaseResult:
    case_id: str
    family: str
    metrics: dict[str, float]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    parse_error: str | None = None


@dataclass
class ModelRun:
    model_alias: str
    model_id: str
    case_results: list[ModelCaseResult]
    aggregates: dict[str, float]
    total_cost_usd: float
    total_latency_ms: float
    parse_failures: int


def _summarize_investigation(report: RcaReport) -> str:
    if report.investigation is None or not report.investigation.tool_calls:
        return "(no tool calls recorded)"
    lines: list[str] = []
    for tc in report.investigation.tool_calls[:8]:
        svc = tc.input.get("service", "?") if isinstance(tc.input, dict) else "?"
        lines.append(f"- {tc.tool}(service={svc}): {tc.result_summary}")
    return "\n".join(lines)


def _topology_hint(report: RcaReport) -> str:
    services: set[str] = set()
    for h in report.hypotheses:
        services.add(h.root_cause_service)
    services.add(report.alert.service)
    return ", ".join(sorted(services))


def _build_prompt(alert: Alert, expected: RcaReport) -> str:
    return _USER_TEMPLATE.format(
        alert_json=json.dumps(alert.model_dump(mode="json"), indent=2),
        investigation_summary=_summarize_investigation(expected),
        topology_hint=_topology_hint(expected),
    )


def _parse_response(
    raw_text: str, alert: Alert, model_id: str
) -> tuple[RcaReport | None, str | None]:
    """Return the parsed report or (None, error_message)."""
    if not raw_text:
        return None, "empty response"
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"
    try:
        confidence = float(payload.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        rca = RcaReport(
            report_id=f"live-{alert.alert_id}",
            tenant_id=alert.tenant_id,
            alert=alert,
            generated_at=datetime.now(timezone.utc),
            model=ModelRef(provider="anthropic", id=model_id),
            hypotheses=[
                Hypothesis(
                    root_cause_service=str(payload["root_cause_service"]),
                    confidence=confidence,
                    reasoning=str(payload.get("reasoning") or ""),
                    evidence=[
                        EvidenceItem(
                            claim=str(payload.get("reasoning") or "no reasoning provided"),
                            source="tool_calls[0]",
                        )
                    ],
                    recommended_action=str(payload.get("recommended_action") or "(unspecified)"),
                )
            ],
        )
    except (KeyError, ValueError, TypeError) as exc:
        return None, f"schema mismatch: {exc}"
    return rca, None


def _client_for(model_alias: str) -> LlmClient:
    spec = CATALOG.get(model_alias)
    if spec is None:
        raise ValueError(f"unknown model alias: {model_alias}. Known: {list(CATALOG)}")
    if spec["provider"] == "anthropic":
        return AnthropicLlm(model_alias=model_alias, cost_ceiling_usd=10.0)
    return MockLlm()


def predict_one(
    alert: Alert,
    expected: RcaReport,
    client: LlmClient,
    *,
    model_alias: str,
) -> tuple[RcaReport, dict[str, float], str | None]:
    """Single-shot live prediction. Returns (predicted_report, telemetry, parse_error)."""
    prompt = _build_prompt(alert, expected)
    started = datetime.now(timezone.utc)
    response = client.generate(prompt, max_tokens=400, system=_SYSTEM_PROMPT, expect_json=True)
    elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
    tokens_in = int(response.get("tokens_in", 0))
    tokens_out = int(response.get("tokens_out", 0))
    cost = float(response.get("cost_usd", estimate_cost(model_alias, tokens_in, tokens_out)))

    parsed, error = _parse_response(response.get("text", ""), alert, CATALOG[model_alias]["id"])
    if parsed is None:
        # Fall back to a sentinel report so scoring stays well-defined.
        parsed = RcaReport(
            report_id=f"live-{alert.alert_id}",
            tenant_id=alert.tenant_id,
            alert=alert,
            generated_at=datetime.now(timezone.utc),
            model=ModelRef(provider="anthropic", id=CATALOG[model_alias]["id"]),
            hypotheses=[
                Hypothesis(
                    root_cause_service="(unknown)",
                    confidence=0.0,
                    reasoning="(parse failure)",
                    evidence=[EvidenceItem(claim="parse failure", source="tool_calls[0]")],
                    recommended_action="(none)",
                )
            ],
        )
    telemetry = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "latency_ms": elapsed_ms,
    }
    return parsed, telemetry, error


def run_model(
    model_alias: str,
    cases: Iterable[tuple[str, str, Alert, RcaReport]],
) -> ModelRun:
    """Score one model across all (case_id, family, alert, expected) cases."""
    client = _client_for(model_alias)
    case_results: list[ModelCaseResult] = []
    total_cost = 0.0
    total_latency = 0.0
    parse_failures = 0

    for case_id, family, alert, expected in cases:
        predicted, tel, err = predict_one(alert, expected, client, model_alias=model_alias)
        metrics = score_all(predicted, expected)
        case_results.append(
            ModelCaseResult(
                case_id=case_id,
                family=family,
                metrics=metrics,
                tokens_in=tel["tokens_in"],
                tokens_out=tel["tokens_out"],
                cost_usd=tel["cost_usd"],
                latency_ms=tel["latency_ms"],
                parse_error=err,
            )
        )
        total_cost += tel["cost_usd"]
        total_latency += tel["latency_ms"]
        if err is not None:
            parse_failures += 1

    aggregates: dict[str, float] = {}
    if case_results:
        keys = list(case_results[0].metrics.keys())
        aggregates = {k: sum(r.metrics[k] for r in case_results) / len(case_results) for k in keys}
    return ModelRun(
        model_alias=model_alias,
        model_id=CATALOG[model_alias]["id"],
        case_results=case_results,
        aggregates=aggregates,
        total_cost_usd=total_cost,
        total_latency_ms=total_latency,
        parse_failures=parse_failures,
    )


def render_markdown(runs: list[ModelRun]) -> str:
    """Markdown table comparing models on the synthetic track."""
    if not runs:
        return "(no model runs)"
    metric_keys = [
        "component_match",
        "top_3_accuracy",
        "reason_cosine",
        "escalation_precision",
    ]
    headers = (
        ["model", "id", "n"]
        + [k.replace("_", " ") for k in metric_keys]
        + ["avg cost / case", "avg latency / case", "parse fails"]
    )
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for run in runs:
        n = len(run.case_results)
        avg_cost = (run.total_cost_usd / n) if n else 0.0
        avg_lat = (run.total_latency_ms / n) if n else 0.0
        cells = [
            run.model_alias,
            f"`{run.model_id}`",
            str(n),
            *[f"{run.aggregates.get(k, 0.0):.2f}" for k in metric_keys],
            f"${avg_cost:.4f}",
            f"{avg_lat:.0f}ms",
            str(run.parse_failures),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


__all__ = [
    "ModelCaseResult",
    "ModelRun",
    "predict_one",
    "render_markdown",
    "run_model",
]
