"""Stage 5 — SYNTHESIZE. Single-shot baseline.

The agent receives the alert and a pre-fetched context bundle, then asks the
LLM for a ranked RCA in one call. PLAN+INVESTIGATE in stage 6 wraps this with
a tool-using loop; the synthesize step itself stays single-shot so the loop
adds tool calls, not extra LLM round-trips.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from jsonschema import ValidationError

from ai_oncall.agent.observability import LlmTracer
from ai_oncall.agent.prompts import synthesize_v1
from ai_oncall.llm.client import LlmClient
from ai_oncall.llm.registry import CATALOG, estimate_cost
from ai_oncall.models import Alert, Investigation, ModelRef, RcaReport, ToolCallRecord
from ai_oncall.schema_loader import validate
from ai_oncall.settings import settings


def _model_ref() -> ModelRef:
    spec = CATALOG.get(_model_alias()) or CATALOG["mock"]
    return ModelRef(provider=spec["provider"], id=spec["id"])  # type: ignore[arg-type]


def _model_alias() -> str:
    for alias, spec in CATALOG.items():
        if spec["id"] == settings.rca_model:
            return alias
    return "mock"


def _build_prompt(alert: Alert, context: dict[str, Any]) -> str:
    return synthesize_v1.SYSTEM_PROMPT + "\n\n" + synthesize_v1.USER_PROMPT_TEMPLATE.format(
        alert_json=alert.model_dump_json(by_alias=True),
        context_json=json.dumps(context, default=str),
    )


def synthesize(
    alert: Alert,
    context: dict[str, Any],
    llm: LlmClient,
    *,
    tool_calls: list[ToolCallRecord] | None = None,
    tracer: LlmTracer | None = None,
) -> RcaReport:
    """Run the SYNTHESIZE stage. Returns an RcaReport that has already been
    schema-validated. Raises ValidationError if the LLM produced malformed JSON.

    `tool_calls` is the trace from stage 4 INVESTIGATE; pass None for the
    single-shot baseline. `tracer`, when supplied, captures every LLM call
    made across the pipeline; the records are attached to
    `report.investigation.llm_calls`.
    """
    prompt = _build_prompt(alert, context)
    if tracer is not None:
        response = tracer.call(
            llm, prompt, stage="synthesize", prompt_version="synthesize_v1", max_tokens=2048
        )
    else:
        response = llm.generate(prompt, max_tokens=2048)

    text = response.get("text", "").strip()
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise ValidationError(f"LLM returned non-JSON: {exc}") from exc

    payload.setdefault("report_id", str(uuid.uuid4()))
    payload.setdefault("tenant_id", alert.tenant_id)
    payload.setdefault("alert", alert.model_dump(mode="json", by_alias=True, exclude_none=True))
    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("model", _model_ref().model_dump())

    cost = estimate_cost(_model_alias(), response.get("tokens_in", 0), response.get("tokens_out", 0))
    llm_call_records = list(tracer.records) if tracer is not None else []
    # The investigation field reflects what the agent ACTUALLY ran, not what
    # the LLM may have hallucinated. Overwrite if tool_calls were supplied.
    if tool_calls is not None:
        payload["investigation"] = Investigation(
            tool_calls=tool_calls,
            tokens_in=response.get("tokens_in", 0),
            tokens_out=response.get("tokens_out", 0),
            cost_usd=cost,
            llm_calls=llm_call_records,
        ).model_dump(mode="json")
    else:
        payload.setdefault(
            "investigation",
            Investigation(
                tool_calls=[],
                tokens_in=response.get("tokens_in", 0),
                tokens_out=response.get("tokens_out", 0),
                cost_usd=cost,
                llm_calls=llm_call_records,
            ).model_dump(mode="json"),
        )
    if cost > settings.cost_ceiling_usd:
        raise RuntimeError(f"cost ${cost:.4f} exceeds ceiling ${settings.cost_ceiling_usd:.2f}")

    validate("rca_report", payload)
    return RcaReport.model_validate(payload)
