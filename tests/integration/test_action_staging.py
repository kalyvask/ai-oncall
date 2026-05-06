"""Action staging — tier classification and kind inference.

Pure-function tests over RcaReport plus one round-trip via JSON Schema to
prove the new optional `staged_action` field validates.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_oncall.agent.staging import (
    AUTO_THRESHOLD,
    PROPOSE_THRESHOLD,
    stage_actions,
)
from ai_oncall.models import (
    Alert,
    AlertSignal,
    EvidenceItem,
    Hypothesis,
    ModelRef,
    RcaReport,
    StagedAction,
)
from ai_oncall.schema_loader import validate

T0 = datetime(2026, 4, 25, 2, 0, tzinfo=timezone.utc)
TENANT = "alpha"


def _alert(severity: str = "page") -> Alert:
    return Alert(
        alert_id="a1", tenant_id=TENANT, fired_at=T0,
        source="manual", severity=severity,  # type: ignore[arg-type]
        service="checkout",
        signal=AlertSignal(kind="manual"),
        title="checkout slow",
    )


def _hypothesis(action: str, confidence: float, service: str = "payment") -> Hypothesis:
    return Hypothesis(
        root_cause_service=service,
        confidence=confidence,
        reasoning="r",
        evidence=[EvidenceItem(claim="c", source="s")],
        recommended_action=action,
    )


def _report(hs: list[Hypothesis], severity: str = "page") -> RcaReport:
    return RcaReport(
        report_id="r1", tenant_id=TENANT, alert=_alert(severity),
        generated_at=T0, model=ModelRef(provider="mock", id="mock"),
        hypotheses=hs,
    )


# --- kind inference -------------------------------------------------------


@pytest.mark.parametrize("text,expected_kind", [
    ("git revert abc1234", "rollback"),
    ("kubectl rollout undo deploy/payment", "rollback"),
    ("Roll back the last deploy", "rollback"),
    ("kubectl rollout restart deploy/payment", "restart"),
    ("restart the payment pods", "restart"),
    ("Scale up to 8 replicas", "scale"),
    ("kubectl scale --replicas=4 deploy/payment", "scale"),
    ("Disable feature flag checkout.async-batching", "feature_flag"),
    ("Toggle off the experimental flag", "feature_flag"),
    ("Wait and see; monitor only", "noop"),
    ("Investigate logs and decide", "manual"),
])
def test_kind_inference(text: str, expected_kind: str) -> None:
    report = _report([_hypothesis(text, confidence=0.9)])
    out = stage_actions(report)
    assert out.hypotheses[0].staged_action is not None
    assert out.hypotheses[0].staged_action.kind == expected_kind


# --- tier classification --------------------------------------------------


def test_auto_tier_for_high_confidence_rollback_on_page() -> None:
    report = _report([_hypothesis("rollback to prior deploy", confidence=0.95)])
    out = stage_actions(report)
    sa = out.hypotheses[0].staged_action
    assert sa is not None
    assert sa.tier == "auto"
    assert sa.kind == "rollback"
    assert "auto-execute whitelist" in (sa.rationale or "")


def test_propose_tier_for_high_confidence_non_whitelisted_kind() -> None:
    # restart is not in the auto whitelist; high confidence still gets propose.
    report = _report([_hypothesis("kubectl rollout restart deploy/payment", confidence=0.95)])
    out = stage_actions(report)
    sa = out.hypotheses[0].staged_action
    assert sa is not None
    assert sa.tier == "propose"


def test_propose_tier_for_medium_confidence_rollback() -> None:
    report = _report([_hypothesis("rollback the last deploy", confidence=0.6)])
    out = stage_actions(report)
    assert out.hypotheses[0].staged_action.tier == "propose"  # type: ignore[union-attr]


def test_recommend_tier_for_low_confidence() -> None:
    report = _report([_hypothesis("rollback the last deploy", confidence=0.2)])
    out = stage_actions(report)
    assert out.hypotheses[0].staged_action.tier == "recommend"  # type: ignore[union-attr]


def test_auto_blocked_on_warn_severity() -> None:
    """Even with a high-confidence rollback, warn-severity alerts should not
    auto-execute. Page severity is the gate."""
    report = _report(
        [_hypothesis("rollback the last deploy", confidence=0.95)],
        severity="warn",
    )
    out = stage_actions(report)
    sa = out.hypotheses[0].staged_action
    assert sa is not None
    assert sa.tier == "propose"


def test_auto_threshold_boundary() -> None:
    just_below = _hypothesis("rollback now", confidence=AUTO_THRESHOLD - 0.001)
    just_above = _hypothesis("rollback now", confidence=AUTO_THRESHOLD)
    out = stage_actions(_report([just_below, just_above]))
    assert out.hypotheses[0].staged_action.tier == "propose"  # type: ignore[union-attr]
    assert out.hypotheses[1].staged_action.tier == "auto"  # type: ignore[union-attr]


def test_propose_threshold_boundary() -> None:
    just_below = _hypothesis("manual investigation", confidence=PROPOSE_THRESHOLD - 0.001)
    just_above = _hypothesis("manual investigation", confidence=PROPOSE_THRESHOLD)
    out = stage_actions(_report([just_below, just_above]))
    assert out.hypotheses[0].staged_action.tier == "recommend"  # type: ignore[union-attr]
    assert out.hypotheses[1].staged_action.tier == "propose"  # type: ignore[union-attr]


def test_existing_staged_action_not_overwritten() -> None:
    h = _hypothesis("rollback", confidence=0.95)
    h.staged_action = StagedAction(
        kind="manual", service="payment", tier="recommend", rationale="LLM-set"
    )
    out = stage_actions(_report([h]))
    assert out.hypotheses[0].staged_action is not None
    assert out.hypotheses[0].staged_action.tier == "recommend"
    assert out.hypotheses[0].staged_action.rationale == "LLM-set"


def test_runbook_link_propagates_to_runbook_ref() -> None:
    h = _hypothesis("rollback now", confidence=0.95)
    h.runbook_link = "runbooks/payment-rollback.md"
    out = stage_actions(_report([h]))
    assert out.hypotheses[0].staged_action.runbook_ref == "runbooks/payment-rollback.md"  # type: ignore[union-attr]


# --- schema validation ----------------------------------------------------


def test_staged_action_passes_rca_schema() -> None:
    report = _report([_hypothesis("rollback now", confidence=0.95)])
    out = stage_actions(report)
    payload = out.model_dump(mode="json", by_alias=True, exclude_none=True)
    validate("rca_report", payload)
    assert payload["hypotheses"][0]["staged_action"]["tier"] == "auto"


def test_existing_reports_without_staged_action_still_valid() -> None:
    """Backward compatibility: rca_report.json's staged_action is optional."""
    report = _report([_hypothesis("rollback", confidence=0.95)])
    payload = report.model_dump(mode="json", by_alias=True, exclude_none=True)
    # No staging called yet — staged_action is absent.
    assert "staged_action" not in payload["hypotheses"][0]
    validate("rca_report", payload)
