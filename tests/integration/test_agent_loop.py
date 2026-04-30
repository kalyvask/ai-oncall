"""Full agent loop end-to-end with MockLlm.

The mock returns a deterministic plan keyed off the PLAN system prompt prefix
and a deterministic synthesized report keyed off the SYNTHESIZE prompt prefix.
The loop must:
  - validate the plan against schemas/investigation_plan.json,
  - exhaust planned queries up to the 8-call budget,
  - hand the trace + bundle to SYNTHESIZE,
  - emit a schema-valid RcaReport with the trace attached.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_oncall.agent.prompts import plan_v1, synthesize_v1
from ai_oncall.agent.run import run_rca
from ai_oncall.agent.tools import MAX_TOOL_CALLS_PER_INCIDENT
from ai_oncall.llm.client import MockLlm
from ai_oncall.models import Alert
from ai_oncall.schema_loader import validate
from ai_oncall.storage.sqlite import SqliteStore

REPO = Path(__file__).resolve().parents[2]


def _mock_with(plan_payload: dict, report_payload: dict) -> MockLlm:
    return MockLlm(fixtures={
        plan_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(plan_payload), "tokens_in": 800, "tokens_out": 200},
        synthesize_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(report_payload), "tokens_in": 4000, "tokens_out": 600},
    })


def test_full_loop_produces_schema_valid_report(tmp_path) -> None:
    alert = Alert.model_validate_json(
        (REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )

    plan_payload = {
        "tenant_id": alert.tenant_id,
        "alert_id": alert.alert_id,
        "hypotheses": [
            {"statement": "payment regression", "confidence": 0.7, "queries": [
                {"tool": "get_topology", "input": {"service": "checkout", "depth": 2}},
                {"tool": "get_recent_deploys", "input": {"service": "payment", "since": "2026-04-24T03:14:00Z"}},
            ]},
            {"statement": "checkout self", "confidence": 0.2, "queries": [
                {"tool": "get_recent_deploys", "input": {"service": "checkout", "since": "2026-04-24T03:14:00Z"}},
            ]},
            {"statement": "external stripe", "confidence": 0.1, "queries": [
                {"tool": "get_runbook", "input": {"service": "payment"}},
            ]},
        ],
    }

    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    mock = _mock_with(plan_payload, expected)

    report = run_rca(alert, store, mock)

    validate("rca_report", report.model_dump(mode="json", by_alias=True, exclude_none=True))
    assert report.hypotheses[0].root_cause_service == "payment"
    assert report.investigation is not None
    assert len(report.investigation.tool_calls) <= MAX_TOOL_CALLS_PER_INCIDENT
    # Plan had 4 queries, all should have been executed.
    assert len(report.investigation.tool_calls) == 4
    # Every recorded tool call resolves to one of the 6 tool names.
    assert {tc.tool for tc in report.investigation.tool_calls} <= {
        "query_metrics", "query_logs", "get_recent_deploys",
        "get_runbook", "get_topology", "get_past_incidents",
    }
