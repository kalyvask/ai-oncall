"""End-to-end tests for stages 1 (RECEIVE) and 2 (ASSEMBLE) via FastAPI."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ai_oncall.server import app

REPO = Path(__file__).resolve().parents[2]
client = TestClient(app)


def test_health_does_not_require_tenant() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_alert_webhook_requires_tenant_header() -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r = client.post("/webhooks/alert", json=payload)
    assert r.status_code == 400
    assert "X-Tenant-Id" in r.text


def test_alert_webhook_normalizes_payload() -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    r = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["service"] == "checkout"
    assert body["tenant_id"] == "demo"
    assert body["signal"]["agg"] == "p99"


def test_alert_webhook_rejects_cross_tenant_payload() -> None:
    payload = json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())
    payload["tenant_id"] = "demo"
    r = client.post("/webhooks/alert", json=payload, headers={"X-Tenant-Id": "other"})
    assert r.status_code == 400
    assert "tenant_id" in r.text


def test_topology_returns_static_yaml_for_tenant() -> None:
    r = client.get("/topology", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "demo"
    services = {n["service"] for n in body["nodes"]}
    assert "checkout" in services
    assert "payment" in services
