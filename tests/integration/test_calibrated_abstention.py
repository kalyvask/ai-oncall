"""Deterministic abstention rules.

Each rule fires independently. The combined ``calibrate()`` collects every
triggered rule, forces ``escalation.should_escalate=True``, and caps the
top hypothesis confidence on cold_start / budget_exhausted to give the
Slack/HTML surfaces a clear "low confidence" signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_oncall.agent.calibration import (
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_LOW_CONVERGENCE_FLOOR,
    DEFAULT_TWO_LEADS_FLOOR,
    calibrate,
)
from ai_oncall.models import RcaReport

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


def test_no_rules_fire_when_signals_are_strong() -> None:
    report = _report()
    # Bump top confidence well above the floor so no rule fires.
    h = report.hypotheses[0].model_copy(update={"confidence": 0.85})
    report = report.model_copy(update={"hypotheses": [h, *report.hypotheses[1:]]})

    recent_deploys = [
        {"service": h.root_cause_service, "timestamp": datetime.now(timezone.utc).isoformat()}
    ]
    new, result = calibrate(
        report,
        recent_deploys=recent_deploys,
        past_incidents=[{"root_cause_service": h.root_cause_service, "root_cause_class": "deploy_regression"}],
        tool_calls_used=4,
    )
    assert result.abstain is False
    assert result.codes == ()
    # The report is returned unchanged.
    assert new.report_id == report.report_id


def test_confidence_floor_fires_below_threshold() -> None:
    report = _report()
    h = report.hypotheses[0].model_copy(update={"confidence": DEFAULT_CONFIDENCE_FLOOR - 0.05})
    report = report.model_copy(update={"hypotheses": [h, *report.hypotheses[1:]]})

    new, result = calibrate(
        report,
        recent_deploys=[{"service": h.root_cause_service, "timestamp": datetime.now(timezone.utc).isoformat()}],
        past_incidents=[{"root_cause_service": h.root_cause_service, "root_cause_class": "x"}],
        tool_calls_used=1,
    )
    assert result.abstain is True
    assert "confidence_floor" in result.codes
    assert new.escalation is not None and new.escalation.should_escalate is True


def test_cold_start_fires_when_no_deploys_and_no_history() -> None:
    report = _report()
    # Fresh confidence so the floor rule doesn't also fire.
    h = report.hypotheses[0].model_copy(update={"confidence": 0.7})
    report = report.model_copy(update={"hypotheses": [h, *report.hypotheses[1:]]})

    new, result = calibrate(
        report,
        recent_deploys=[],  # no deploys
        past_incidents=[],  # no past incidents
        tool_calls_used=2,
    )
    assert result.abstain is True
    assert "cold_start" in result.codes
    # cold_start caps top confidence so Slack shows "low confidence."
    assert new.hypotheses[0].confidence <= 0.40


def test_budget_exhausted_fires_when_tool_cap_hit_without_convergence() -> None:
    report = _report()
    h = report.hypotheses[0].model_copy(update={"confidence": DEFAULT_LOW_CONVERGENCE_FLOOR - 0.05})
    report = report.model_copy(update={"hypotheses": [h, *report.hypotheses[1:]]})

    new, result = calibrate(
        report,
        recent_deploys=[{"service": h.root_cause_service, "timestamp": datetime.now(timezone.utc).isoformat()}],
        past_incidents=[{"root_cause_service": h.root_cause_service, "root_cause_class": "x"}],
        tool_calls_used=8,  # exhausted
    )
    assert "budget_exhausted" in result.codes
    assert new.escalation is not None


def test_two_strong_leads_fires_on_competing_services() -> None:
    report = _report()
    # Force two hypotheses on different services both above the two-leads floor.
    h1 = report.hypotheses[0].model_copy(
        update={
            "root_cause_service": "checkout",
            "confidence": DEFAULT_TWO_LEADS_FLOOR + 0.10,
        }
    )
    if len(report.hypotheses) >= 2:
        h2 = report.hypotheses[1].model_copy(
            update={
                "root_cause_service": "payment",
                "confidence": DEFAULT_TWO_LEADS_FLOOR + 0.10,
            }
        )
        new_h = [h1, h2, *report.hypotheses[2:]]
    else:
        new_h = [h1]
    report = report.model_copy(update={"hypotheses": new_h})

    new, result = calibrate(
        report,
        recent_deploys=[{"service": "checkout", "timestamp": datetime.now(timezone.utc).isoformat()}],
        past_incidents=[{"root_cause_service": "checkout", "root_cause_class": "x"}],
        tool_calls_used=4,
    )
    assert "two_strong_leads" in result.codes


def test_two_leads_does_not_fire_for_same_service() -> None:
    """Two hypotheses on the same service are competing on phrasing, not
    independent. They should not trigger the tie rule."""
    report = _report()
    h1 = report.hypotheses[0].model_copy(
        update={"root_cause_service": "x", "confidence": 0.8}
    )
    h2 = (
        report.hypotheses[1].model_copy(
            update={"root_cause_service": "x", "confidence": 0.7}
        )
        if len(report.hypotheses) >= 2
        else h1
    )
    report = report.model_copy(update={"hypotheses": [h1, h2, *report.hypotheses[2:]]})

    _, result = calibrate(
        report,
        recent_deploys=[{"service": "x", "timestamp": datetime.now(timezone.utc).isoformat()}],
        past_incidents=[{"root_cause_service": "x", "root_cause_class": "x"}],
        tool_calls_used=3,
    )
    assert "two_strong_leads" not in result.codes
