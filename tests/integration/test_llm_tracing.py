"""LlmTracer + Investigation.llm_calls.

Three layers:
  1. LlmTracer captures one record per generate() call with stage,
     prompt_version, prompt hash, model id, tokens, cost, latency.
  2. plan() and synthesize() append their own records when given a tracer.
  3. run_rca() ends up with both records on the final report's investigation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_oncall.agent.observability import LlmTracer
from ai_oncall.agent.prompts import plan_v1, synthesize_v1
from ai_oncall.agent.run import run_rca
from ai_oncall.llm.client import MockLlm
from ai_oncall.models import Alert
from ai_oncall.schema_loader import validate
from ai_oncall.storage.sqlite import SqliteStore

REPO = Path(__file__).resolve().parents[2]


# --- 1. tracer mechanics ---------------------------------------------------


def test_tracer_records_a_call_with_prompt_hash_and_latency() -> None:
    tracer = LlmTracer()
    llm = MockLlm(fixtures={"hello": {"text": "world", "tokens_in": 10, "tokens_out": 5}})
    response = tracer.call(
        llm, "hello world", stage="plan", prompt_version="plan_v1"
    )
    assert response["text"] == "world"
    assert len(tracer.records) == 1
    record = tracer.records[0]
    assert record.stage == "plan"
    assert record.prompt_version == "plan_v1"
    assert record.prompt_hash == hashlib.sha256(b"hello world").hexdigest()[:16]
    assert record.tokens_in == 10
    assert record.tokens_out == 5
    assert record.latency_ms >= 0
    assert record.error is None
    assert isinstance(record.started_at, datetime)


def test_tracer_records_error_on_exception() -> None:
    tracer = LlmTracer()

    class _Boom:
        def generate(self, prompt: str, **_: object) -> dict:
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        tracer.call(_Boom(), "p", stage="plan", prompt_version="plan_v1")
    assert len(tracer.records) == 1
    assert tracer.records[0].error == "network down"
    assert tracer.records[0].tokens_in is None


def test_tracer_accumulates_across_calls() -> None:
    tracer = LlmTracer()
    llm = MockLlm()  # default empty
    tracer.call(llm, "first", stage="plan", prompt_version="plan_v1")
    tracer.call(llm, "second", stage="synthesize", prompt_version="synthesize_v1")
    assert [r.stage for r in tracer.records] == ["plan", "synthesize"]


# --- 2. integration with run_rca -------------------------------------------


def _mock_with(plan_payload: dict, report_payload: dict) -> MockLlm:
    return MockLlm(fixtures={
        plan_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(plan_payload), "tokens_in": 800, "tokens_out": 200},
        synthesize_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(report_payload), "tokens_in": 4000, "tokens_out": 600},
    })


def test_run_rca_attaches_two_llm_calls_to_investigation(tmp_path) -> None:
    alert = Alert.model_validate_json(
        (REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    plan_payload = {
        "tenant_id": alert.tenant_id, "alert_id": alert.alert_id,
        "hypotheses": [
            {"statement": "payment regression", "confidence": 0.7, "queries": [
                {"tool": "get_recent_deploys", "input": {"service": "payment", "since": "2026-04-24T03:14:00Z"}},
            ]},
            {"statement": "self regression", "confidence": 0.3, "queries": [
                {"tool": "get_recent_deploys", "input": {"service": "checkout", "since": "2026-04-24T03:14:00Z"}},
            ]},
            {"statement": "external stripe", "confidence": 0.1, "queries": [
                {"tool": "get_runbook", "input": {"service": "payment"}},
            ]},
        ],
    }
    mock = _mock_with(plan_payload, expected)
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))

    report = run_rca(alert, store, mock)

    assert report.investigation is not None
    assert len(report.investigation.llm_calls) == 2
    stages = [r.stage for r in report.investigation.llm_calls]
    assert stages == ["plan", "synthesize"]
    plan_record = report.investigation.llm_calls[0]
    assert plan_record.tokens_in == 800
    assert plan_record.tokens_out == 200
    syn_record = report.investigation.llm_calls[1]
    assert syn_record.tokens_in == 4000
    assert syn_record.tokens_out == 600
    # Each record carries a prompt hash that is stable per-prompt.
    assert plan_record.prompt_hash != syn_record.prompt_hash


def test_run_rca_report_with_llm_calls_is_schema_valid(tmp_path) -> None:
    """The new llm_calls field round-trips through the rca_report.json schema."""
    alert = Alert.model_validate_json(
        (REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    plan_payload = {
        "tenant_id": alert.tenant_id, "alert_id": alert.alert_id,
        "hypotheses": [
            {"statement": "h1", "confidence": 0.7, "queries": [
                {"tool": "get_runbook", "input": {"service": "payment"}},
            ]},
            {"statement": "h2", "confidence": 0.5, "queries": [
                {"tool": "get_runbook", "input": {"service": "checkout"}},
            ]},
            {"statement": "h3", "confidence": 0.3, "queries": [
                {"tool": "get_runbook", "input": {"service": "cart"}},
            ]},
        ],
    }
    mock = _mock_with(plan_payload, expected)
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    report = run_rca(alert, store, mock)
    payload = report.model_dump(mode="json", by_alias=True, exclude_none=True)
    validate("rca_report", payload)
    assert "llm_calls" in payload["investigation"]
    assert len(payload["investigation"]["llm_calls"]) == 2


def test_old_reports_without_llm_calls_still_validate() -> None:
    """Backward compatibility: rca_report.json's llm_calls is optional."""
    expected = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    # The shipped fixtures pre-date llm_calls; they must still validate.
    validate("rca_report", expected)
