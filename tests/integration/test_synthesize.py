"""SYNTHESIZE single-shot baseline. The MockLlm returns the expected fixture
verbatim; the test checks that the synthesizer plumbs everything end-to-end and
produces a schema-valid report.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_oncall.agent.synthesize import synthesize
from ai_oncall.llm.client import MockLlm
from ai_oncall.models import Alert
from ai_oncall.schema_loader import validate

REPO = Path(__file__).resolve().parents[2]


def test_synthesize_returns_schema_valid_report() -> None:
    alert_path = REPO / "fixtures/synthetic_alerts/checkout_regression.json"
    expected_path = REPO / "fixtures/expected_reports/checkout_regression.json"
    alert = Alert.model_validate_json(alert_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    # Mock returns the canonical expected report when prompted with anything
    # starting with the synthesize system prompt.
    from ai_oncall.agent.prompts import synthesize_v1

    expected_text = json.dumps(expected)
    mock = MockLlm(
        fixtures={
            synthesize_v1.SYSTEM_PROMPT[:60]: {
                "text": expected_text,
                "tokens_in": 4812,
                "tokens_out": 612,
            }
        }
    )

    report = synthesize(
        alert, context={"deploys": [], "topology": {}, "logs": [], "metrics": []}, llm=mock
    )
    validate("rca_report", report.model_dump(mode="json", by_alias=True, exclude_none=True))
    assert report.hypotheses[0].root_cause_service == "payment"
    assert report.hypotheses[0].confidence >= report.hypotheses[-1].confidence


def test_synthesize_falls_back_to_uuid_and_now() -> None:
    """If the LLM omits report_id / generated_at, synthesize fills them in."""
    alert = Alert.model_validate_json(
        (REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    expected.pop("report_id")
    expected.pop("generated_at")
    from ai_oncall.agent.prompts import synthesize_v1

    mock = MockLlm(
        fixtures={
            synthesize_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(expected),
                "tokens_in": 100,
                "tokens_out": 50,
            }
        }
    )

    report = synthesize(alert, context={}, llm=mock)
    assert report.report_id  # generated
    assert report.generated_at  # filled in
