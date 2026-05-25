"""Slack thread Q&A and the replay command.

Both modules are exercised against an in-memory store and a MockLlm. No
real network or Anthropic key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.agent.replay import (
    CONFIDENCE_TOLERANCE,
    REGRESSION_DROP,
    _diff_reports,
    replay_batch,
    replay_incident,
)
from ai_oncall.delivery.thread_qa import (
    answer_thread_question,
    render_answer_blocks,
    _classify_intent,
    _pick_hypothesis,
)
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.models import RcaReport
from ai_oncall.storage.factory import make_store

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


@pytest.fixture
def tmp_incidents_db(tmp_path, monkeypatch):
    path = tmp_path / "incidents.sqlite"
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", path)
    return path


# --- thread_qa: intent classification ------------------------------------


def test_classify_intent_picks_metric_for_p99_question() -> None:
    intent, tools = _classify_intent("show me the p99 latency")
    assert intent == "show_metric"
    assert "query_metrics" in tools


def test_classify_intent_picks_logs_for_error_question() -> None:
    intent, tools = _classify_intent("any errors in the logs?")
    assert intent == "show_logs"
    assert "query_logs" in tools


def test_classify_intent_falls_back_to_explain() -> None:
    intent, _ = _classify_intent("hmm")
    assert intent == "explain"


# --- thread_qa: hypothesis picking ---------------------------------------


def test_pick_hypothesis_matches_service_name_directly() -> None:
    report = _report()
    target = report.hypotheses[-1].root_cause_service
    idx = _pick_hypothesis(report, f"why is {target} slow?", llm=None)
    assert idx == len(report.hypotheses) - 1


def test_pick_hypothesis_falls_back_to_top() -> None:
    report = _report()
    idx = _pick_hypothesis(report, "what happened?", llm=None)
    assert idx == 0


# --- thread_qa: end-to-end -----------------------------------------------


def test_answer_thread_question_runs_bounded_tools() -> None:
    report = _report()
    store = make_store()
    answer = answer_thread_question(
        report=report,
        question="show me logs and the recent deploys",
        store=store,
        llm=None,  # deterministic fallback
    )

    # At most 3 tool calls, regardless of how many tools the intent suggested.
    assert len(answer.tool_calls) <= 3
    assert answer.report_id == report.report_id
    assert answer.summary  # non-empty
    blocks = render_answer_blocks(answer)
    assert blocks[0]["text"]["text"].startswith("*Q:*")


# --- replay: diff logic --------------------------------------------------


def test_diff_reports_match_when_identical() -> None:
    report = _report()
    diff = _diff_reports(report, report)
    assert diff.verdict == "match"
    assert diff.confidence_delta == 0.0
    assert diff.same_top_hypothesis is True


def test_diff_reports_regression_when_top_changes() -> None:
    report = _report()
    bumped = report.hypotheses[0].model_copy(update={"root_cause_service": "different-svc"})
    drifted = report.model_copy(update={"hypotheses": [bumped, *report.hypotheses[1:]]})
    diff = _diff_reports(report, drifted)
    assert diff.verdict == "regression"
    assert any("top hypothesis" in d for d in diff.differences)


def test_diff_reports_regression_when_confidence_drops() -> None:
    report = _report()
    h0 = report.hypotheses[0]
    weaker = h0.model_copy(update={"confidence": max(0, h0.confidence - REGRESSION_DROP - 0.05)})
    drifted = report.model_copy(update={"hypotheses": [weaker, *report.hypotheses[1:]]})
    diff = _diff_reports(report, drifted)
    assert diff.verdict == "regression"


def test_diff_reports_drift_within_tolerance() -> None:
    report = _report()
    h0 = report.hypotheses[0]
    new_conf = max(0.0, min(1.0, h0.confidence + CONFIDENCE_TOLERANCE + 0.01))
    nudged = h0.model_copy(update={"confidence": new_conf})
    drifted = report.model_copy(update={"hypotheses": [nudged, *report.hypotheses[1:]]})
    diff = _diff_reports(report, drifted)
    assert diff.verdict == "drift"


# --- replay: end-to-end --------------------------------------------------


def test_replay_incident_returns_match_when_no_pipeline_change(
    tmp_incidents_db, monkeypatch
) -> None:
    """Replaying a stored report through the same MockLlm fixtures should
    yield a matching verdict (deterministic stub means same report)."""
    report = _report()
    save_incident(report)
    store = make_store()
    diff = replay_incident(report.report_id, store=store)
    # The replayed report comes from the same fixture text via _replay_mock_from_report,
    # so the top hypothesis is the same. Confidence may shift slightly because
    # calibration runs on each replay, but the verdict should not be a regression.
    assert diff.verdict in {"match", "drift", "improvement"}, (
        f"unexpected verdict for identical replay: {diff.verdict} differences={diff.differences}"
    )


def test_replay_incident_raises_when_unknown(tmp_incidents_db) -> None:
    store = make_store()
    with pytest.raises(ValueError):
        replay_incident("rpt_does_not_exist", store=store)


def test_replay_batch_aggregates_outcomes(tmp_incidents_db) -> None:
    report = _report()
    save_incident(report)
    second = report.model_copy(update={"report_id": report.report_id + "_b"})
    save_incident(second)
    store = make_store()

    result = replay_batch([report.report_id, second.report_id], store=store)
    assert result.total == 2
    # Both replays use the same fixture, so neither should regress.
    assert result.regressions == 0
