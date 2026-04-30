"""Stage 3 — PLAN. The LLM proposes 3-5 hypotheses and the queries to test
each. Output is an InvestigationPlan that drives stage 4.
"""

from __future__ import annotations

import json

from jsonschema import ValidationError

from ai_oncall.agent.prompts import plan_v1
from ai_oncall.llm.client import LlmClient
from ai_oncall.models import Alert, InvestigationPlan
from ai_oncall.schema_loader import validate


def plan(alert: Alert, llm: LlmClient) -> InvestigationPlan:
    prompt = plan_v1.SYSTEM_PROMPT + "\n\n" + plan_v1.USER_PROMPT_TEMPLATE.format(
        alert_json=alert.model_dump_json(by_alias=True, exclude_none=True),
    )
    response = llm.generate(prompt, max_tokens=1024)
    text = response.get("text", "").strip()
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise ValidationError(f"PLAN returned non-JSON: {exc}") from exc

    payload.setdefault("tenant_id", alert.tenant_id)
    payload.setdefault("alert_id", alert.alert_id)
    validate("investigation_plan", payload)
    return InvestigationPlan.model_validate(payload)
