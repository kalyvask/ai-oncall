"""Model comparison mode — single-shot live prediction + Markdown table.

The real path calls Anthropic, which CI cannot do. Tests inject a stub
``LlmClient`` so the path runs end-to-end without a network call. The
Markdown renderer is exercised against a real ``ModelRun`` so its
structure is pinned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_oncall.models import RcaReport
from evals.model_compare import (
    ModelCaseResult,
    ModelRun,
    predict_one,
    render_markdown,
    run_model,
)

REPO = Path(__file__).resolve().parents[2]


class _StubLlm:
    """Returns a canned JSON response — the same shape the real adapter emits."""

    def __init__(self, *, root_cause: str, confidence: float = 0.91) -> None:
        self.payload = {
            "root_cause_service": root_cause,
            "confidence": confidence,
            "reasoning": "Deploy of the Stripe SDK to payment regressed the call signature.",
            "recommended_action": "git revert abc1234 && deploy payment",
            "should_escalate": False,
        }
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt: str, *, max_tokens: int = 1024, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens, **kwargs})
        return {
            "text": json.dumps(self.payload),
            "tokens_in": 200,
            "tokens_out": 80,
            "cost_usd": 0.0012,
            "model_id": "stub-model",
        }


def _expected() -> RcaReport:
    return RcaReport.model_validate_json(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )


def test_predict_one_returns_scored_report_and_telemetry() -> None:
    expected = _expected()
    llm = _StubLlm(root_cause="payment", confidence=0.91)

    predicted, telemetry, err = predict_one(
        expected.alert, expected, llm, model_alias="claude-haiku"
    )

    assert err is None
    assert predicted.hypotheses[0].root_cause_service == "payment"
    assert 0.0 <= predicted.hypotheses[0].confidence <= 1.0
    assert telemetry["tokens_in"] == 200
    assert telemetry["tokens_out"] == 80
    assert telemetry["cost_usd"] == pytest.approx(0.0012)
    assert telemetry["latency_ms"] >= 0.0


def test_predict_one_handles_malformed_json_without_crashing() -> None:
    """A model that returns prose instead of JSON shouldn't take down the eval."""
    expected = _expected()

    class _BrokenLlm:
        def generate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "text": "I think it's payment.",
                "tokens_in": 50,
                "tokens_out": 10,
                "cost_usd": 0.0001,
            }

    predicted, telemetry, err = predict_one(
        expected.alert, expected, _BrokenLlm(), model_alias="claude-haiku"
    )
    assert err is not None
    assert "JSONDecodeError" in err
    # Sentinel report so component_match scores 0 (rather than crashing the harness).
    assert predicted.hypotheses[0].root_cause_service == "(unknown)"


def test_predict_one_clamps_out_of_range_confidence() -> None:
    expected = _expected()
    llm = _StubLlm(root_cause="payment", confidence=2.5)

    predicted, _, err = predict_one(expected.alert, expected, llm, model_alias="claude-haiku")
    assert err is None
    assert predicted.hypotheses[0].confidence == 1.0


def test_run_model_aggregates_across_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _client_for so run_model doesn't construct a real AnthropicLlm."""
    import evals.model_compare as model_compare

    monkeypatch.setattr(model_compare, "_client_for", lambda alias: _StubLlm(root_cause="payment"))

    expected = _expected()
    cases = [
        ("c1", "deploy_regression", expected.alert, expected),
        ("c2", "deploy_regression", expected.alert, expected),
        ("c3", "deploy_regression", expected.alert, expected),
    ]
    run = run_model("claude-haiku", cases)

    assert len(run.case_results) == 3
    assert run.aggregates["component_match"] == pytest.approx(1.0)
    assert run.parse_failures == 0
    assert run.total_cost_usd == pytest.approx(0.0036)


def test_render_markdown_table_shape() -> None:
    run = ModelRun(
        model_alias="claude-haiku",
        model_id="claude-haiku-4-5-20251001",
        case_results=[
            ModelCaseResult(
                case_id="c1",
                family="deploy_regression",
                metrics={
                    "component_match": 1.0,
                    "top_3_accuracy": 1.0,
                    "reason_cosine": 0.82,
                    "escalation_precision": 1.0,
                },
                tokens_in=200,
                tokens_out=80,
                cost_usd=0.0012,
                latency_ms=820.0,
            )
        ],
        aggregates={
            "component_match": 1.0,
            "top_3_accuracy": 1.0,
            "reason_cosine": 0.82,
            "escalation_precision": 1.0,
        },
        total_cost_usd=0.0012,
        total_latency_ms=820.0,
        parse_failures=0,
    )
    md = render_markdown([run])

    assert "| model |" in md
    assert "claude-haiku" in md
    assert "claude-haiku-4-5-20251001" in md
    assert "1.00" in md
    assert "0.82" in md
    assert "$0.0012" in md
    assert "820ms" in md
    assert "parse fails" in md


def test_harness_model_compare_flag_dispatches(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """End-to-end: --model-compare claude-haiku passes through the harness."""
    import evals.harness as harness
    import evals.model_compare as model_compare

    monkeypatch.setattr(model_compare, "_client_for", lambda alias: _StubLlm(root_cause="payment"))

    rc = harness.main(["--model-compare", "claude-haiku"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "| model |" in out
    assert "claude-haiku-4-5-20251001" in out
