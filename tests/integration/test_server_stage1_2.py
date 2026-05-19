"""End-to-end tests for stages 1 (RECEIVE) and 2 (ASSEMBLE) via FastAPI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_oncall.jobs.store as jobs_store

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Boot the app with the worker disabled (tests drive jobs synchronously
    via ``process_one``) and an isolated jobs DB."""
    monkeypatch.setenv("AI_ONCALL_DISABLE_WORKER", "1")
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    from ai_oncall.server import app

    with TestClient(app) as c:
        yield c


def test_health_does_not_require_tenant(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_ready_reports_jobs_db(client) -> None:
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["jobs_db"] is True


def test_alert_webhook_requires_tenant_header(client) -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r = client.post("/webhooks/alert", json=payload)
    assert r.status_code == 400
    assert "X-Tenant-Id" in r.text


def test_alert_webhook_enqueues_rca_job(client) -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["alert_id"] == payload["alert_id"]
    assert body["status_url"].startswith("/jobs/")
    assert r.headers["location"] == body["status_url"]


def test_alert_webhook_is_idempotent_on_alert_id(client) -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r1 = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "demo"})
    r2 = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "demo"})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]


def test_alert_webhook_rejects_cross_tenant_payload(client) -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    payload["tenant_id"] = "demo"
    r = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "other"})
    assert r.status_code == 400
    assert "tenant_id" in r.text


def test_job_status_endpoint_returns_pending(client) -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "demo"})
    job_id = r.json()["job_id"]
    s = client.get(f"/jobs/{job_id}", headers={"X-Tenant-Id": "demo"})
    assert s.status_code == 200
    body = s.json()
    assert body["status"] == "pending"
    assert body["kind"] == "rca"


def test_job_status_rejects_cross_tenant_read(client) -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "demo"})
    job_id = r.json()["job_id"]
    s = client.get(f"/jobs/{job_id}", headers={"X-Tenant-Id": "other"})
    assert s.status_code == 404


def test_topology_returns_static_yaml_for_tenant(client) -> None:
    r = client.get("/topology", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "demo"
    services = {n["service"] for n in body["nodes"]}
    assert "checkout" in services
    assert "payment" in services
