"""Latency + cost SLO surfacing.

The worker stamps ``total_latency_ms`` and ``slo_violated`` on each saved
report's investigation block. ``GET /sloreport`` aggregates p50/p95 latency
and counts violations against ``latency_budget_seconds``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_oncall.jobs.store as jobs_store
import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.models import RcaReport

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    return RcaReport.model_validate_json(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )


def _stamp_latency(report_id: str, total_latency_ms: float, slo_violated: bool) -> None:
    from ai_oncall.learnings.incidents import _conn

    with _conn() as conn:
        row = conn.execute("SELECT report_json FROM incidents WHERE report_id=?", (report_id,)).fetchone()
        blob = json.loads(row["report_json"])
        inv = blob.setdefault("investigation", {}) or {}
        inv["total_latency_ms"] = total_latency_ms
        inv["slo_violated"] = slo_violated
        blob["investigation"] = inv
        conn.execute("UPDATE incidents SET report_json=? WHERE report_id=?", (json.dumps(blob), report_id))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ONCALL_DISABLE_WORKER", "1")
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    from ai_oncall.server import app

    with TestClient(app) as c:
        yield c


def test_sloreport_returns_zero_samples_when_no_incidents(client) -> None:
    r = client.get("/sloreport", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["incidents_sampled"] == 0
    assert body["latency_ms"]["p50"] is None
    assert body["slo_violations"] == 0


def test_sloreport_aggregates_p50_p95_and_violations(client) -> None:
    # Save 3 incidents with synthetic latencies; two violate a 30s budget.
    base = _report()
    for i, (ms, viol) in enumerate(((10_000, False), (45_000, True), (60_000, True))):
        new_id = f"00000000-0000-0000-0000-{i:012d}"
        r = base.model_copy(update={"report_id": new_id})
        save_incident(r)
        _stamp_latency(new_id, ms, viol)

    resp = client.get("/sloreport", headers={"X-Tenant-Id": "demo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["incidents_sampled"] == 3
    assert body["latency_ms"]["samples"] == 3
    # p50 of [10000, 45000, 60000] sorted is the middle: 45000
    assert body["latency_ms"]["p50"] == 45_000.0
    assert body["latency_ms"]["p95"] == 60_000.0
    assert body["slo_violations"] == 2
