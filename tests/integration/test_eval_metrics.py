"""New eval metrics + real-incident loader.

Covers: top-k accuracy, evidence precision, abstention correctness,
unsafe action rate, plus a smoke test that all 5 starter real-incident
cases load and have the required fields.
"""

from __future__ import annotations

from pathlib import Path

from evals.real_loader import iter_real_cases, score_real_case
from evals.scoring import (
    abstention_correctness,
    evidence_precision,
    score_all,
    top_k_accuracy,
    unsafe_action_rate,
)
from ai_oncall.models import Calibration, EvidenceItem, RcaReport, StagedAction

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    return RcaReport.model_validate_json(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )


def test_top_k_accuracy_hits_when_target_is_in_top_3() -> None:
    expected = _report()
    predicted = _report()
    # Reorder predicted hypotheses; the expected top still appears at index 2.
    rearranged = [predicted.hypotheses[2], predicted.hypotheses[1], predicted.hypotheses[0]]
    predicted = predicted.model_copy(update={"hypotheses": rearranged})
    assert top_k_accuracy(predicted, expected, k=3) == 1.0
    assert top_k_accuracy(predicted, expected, k=1) == 0.0


def test_evidence_precision_rewards_pinned_sources() -> None:
    report = _report()
    h = report.hypotheses[0]
    # Mix of well-pinned + free-text sources.
    new_evidence = [
        EvidenceItem(claim="x", source="tool_calls[0]"),
        EvidenceItem(claim="y", source="sha:abc1234567"),
        EvidenceItem(claim="z", source="from the logs"),  # free text
    ]
    new_h = h.model_copy(update={"evidence": new_evidence})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    p = evidence_precision(report)
    assert 0.65 < p < 0.7  # 2 of 3 pinned


def test_abstention_correctness_matches_decisions() -> None:
    a = _report().model_copy(update={"calibration": Calibration(abstain=True, reasons=[])})
    b = _report().model_copy(update={"calibration": Calibration(abstain=True, reasons=[])})
    assert abstention_correctness(a, b) == 1.0
    c = _report().model_copy(update={"calibration": Calibration(abstain=False, reasons=[])})
    assert abstention_correctness(a, c) == 0.0


def test_unsafe_action_rate_flags_policy_violations() -> None:
    report = _report()
    h = report.hypotheses[0]
    # Force an unsafe (manual + propose) staged action.
    unsafe = StagedAction(kind="manual", service=h.root_cause_service, tier="propose")
    new_h = h.model_copy(update={"staged_action": unsafe})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    rate = unsafe_action_rate(report)
    assert rate > 0


def test_score_all_returns_full_metric_set() -> None:
    report = _report()
    scores = score_all(report, report)
    expected_keys = {
        "component_match",
        "top_3_accuracy",
        "reason_cosine",
        "trajectory_score",
        "escalation_precision",
        "evidence_precision",
        "abstention_correctness",
        "unsafe_action_rate",
    }
    assert expected_keys <= set(scores.keys())


# --- real-incident benchmark ---------------------------------------------


def test_real_incident_cases_load_and_validate() -> None:
    cases = list(iter_real_cases())
    assert len(cases) >= 5, f"expected >= 5 starter cases, got {len(cases)}"
    for case in cases:
        assert case.source_url.startswith("https://"), f"{case.case_id} missing source URL"
        assert case.alert.alert_id
        assert case.expected_top_root_cause_service
        assert case.expected_root_cause_class


def test_score_real_case_recognizes_match() -> None:
    cases = list(iter_real_cases())
    case = cases[0]
    s = score_real_case(case, case.expected_top_root_cause_service, case.expected_root_cause_class)
    assert s == {"service_match": 1.0, "class_match": 1.0}
    s2 = score_real_case(case, "nonsense", "nonsense")
    assert s2 == {"service_match": 0.0, "class_match": 0.0}
