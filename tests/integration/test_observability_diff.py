"""Observability surfaces: incident diff endpoint, Langfuse export no-op."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_oncall.jobs.store as jobs_store
import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.models import ModelRef, RcaReport

REPO = Path(__file__).resolve().parents[2]


def _report(report_id: str, *, model_id: str, top_confidence: float | None = None) -> RcaReport:
    base = RcaReport.model_validate_json(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    updates: dict = {"report_id": report_id, "model": ModelRef(provider="anthropic", id=model_id)}
    if top_confidence is not None:
        h = base.hypotheses[0].model_copy(update={"confidence": top_confidence})
        updates["hypotheses"] = [h, *base.hypotheses[1:]]
    return base.model_copy(update=updates)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ONCALL_DISABLE_WORKER", "1")
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    from ai_oncall.server import app

    with TestClient(app) as c:
        yield c


def test_diff_two_incidents_same_root_cause(client) -> None:
    a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    save_incident(_report(a_id, model_id="claude-haiku-4-5-20251001", top_confidence=0.92))
    save_incident(_report(b_id, model_id="claude-sonnet-4-6", top_confidence=0.88))
    r = client.get(f"/incidents/{a_id}/diff/{b_id}", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["a"]["model_id"] == "claude-haiku-4-5-20251001"
    assert body["b"]["model_id"] == "claude-sonnet-4-6"
    assert body["agreement"]["same_root_cause"] is True
    assert abs(body["agreement"]["confidence_delta"] - 0.04) < 1e-6


def test_diff_rejects_cross_tenant(client) -> None:
    a_id = "11111111-2222-3333-4444-555555555555"
    save_incident(_report(a_id, model_id="claude-haiku-4-5-20251001"))
    # Save second incident under a different tenant.
    other = _report("66666666-7777-8888-9999-aaaaaaaaaaaa", model_id="claude-sonnet-4-6")
    save_incident(other.model_copy(update={"tenant_id": "other"}))
    r = client.get(
        f"/incidents/{a_id}/diff/66666666-7777-8888-9999-aaaaaaaaaaaa",
        headers={"X-Tenant-Id": "demo"},
    )
    assert r.status_code == 404


def test_langfuse_export_is_noop_when_keys_unset(monkeypatch) -> None:
    """The exporter must not raise or block when credentials are missing.
    This guarantees the RCA pipeline never hard-depends on telemetry config."""
    from ai_oncall.agent.observability import _maybe_export_langfuse
    from ai_oncall.models import LlmCallRecord
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "langfuse_public_key", None)
    monkeypatch.setattr(settings, "langfuse_secret_key", None)
    rec = LlmCallRecord(
        stage="plan",
        prompt_version="v1",
        prompt_hash="abc12345",
        model_id="claude-haiku-4-5-20251001",
        latency_ms=120,
    )
    # Should return without raising.
    _maybe_export_langfuse(rec, prompt_hash="abc12345")
