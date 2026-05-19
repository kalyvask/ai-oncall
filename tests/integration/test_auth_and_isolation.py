"""Real auth + tenant isolation.

Three surfaces:

1. ``/webhooks/alert`` HMAC verification. When the signing secret is set,
   unsigned posts get 401; signed posts pass.

2. Per-tenant bearer tokens. When ``tenant_tokens`` is configured, every
   API request must carry the right Bearer token for its tenant.

3. Memory-graph isolation. ``list_root_cause_classes`` and ``list_incidents``
   never return another tenant's rows, even when called with identical
   service names.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_oncall.jobs.store as jobs_store
import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.learnings.incidents import (
    list_incidents,
    list_root_cause_classes,
    save_incident,
)
from ai_oncall.models import RcaReport

REPO = Path(__file__).resolve().parents[2]


def _alert_payload() -> dict:
    return json.loads(
        (REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text()
    )


def _report_for_tenant(tenant: str, report_id: str) -> RcaReport:
    base = RcaReport.model_validate_json(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return base.model_copy(update={"tenant_id": tenant, "report_id": report_id})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ONCALL_DISABLE_WORKER", "1")
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    from ai_oncall.server import app

    with TestClient(app) as c:
        yield c


# --- webhook HMAC --------------------------------------------------------


def test_webhook_accepts_unsigned_request_when_secret_unset(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "webhook_signing_secret", None)
    r = client.post(
        "/webhooks/alert",
        json=_alert_payload(),
        headers={"X-Tenant-Id": "demo"},
    )
    assert r.status_code == 202


def test_webhook_rejects_unsigned_request_when_secret_set(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "webhook_signing_secret", "shh")
    r = client.post(
        "/webhooks/alert",
        json=_alert_payload(),
        headers={"X-Tenant-Id": "demo"},
    )
    assert r.status_code == 401


def test_webhook_accepts_valid_hmac(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "webhook_signing_secret", "shh")
    body = json.dumps(_alert_payload()).encode("utf-8")
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = client.post(
        "/webhooks/alert",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": f"hmac-sha256={sig}",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 202


def test_webhook_rejects_wrong_hmac(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "webhook_signing_secret", "shh")
    body = json.dumps(_alert_payload()).encode("utf-8")
    r = client.post(
        "/webhooks/alert",
        content=body,
        headers={
            "X-Tenant-Id": "demo",
            "X-Signature": "hmac-sha256=deadbeef",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 401


# --- per-tenant bearer tokens -------------------------------------------


def test_bearer_token_required_when_tenant_tokens_configured(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "tenant_tokens", {"demo": "tok_demo"})
    # No Authorization header — must 401.
    r = client.get("/incidents", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 401


def test_bearer_token_must_match_tenant(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "tenant_tokens", {"demo": "tok_demo", "acme": "tok_acme"})
    # Using acme's token on demo's tenant -> 401.
    r = client.get(
        "/incidents",
        headers={"X-Tenant-Id": "demo", "Authorization": "Bearer tok_acme"},
    )
    assert r.status_code == 401


def test_bearer_token_correct_token_passes(client, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "tenant_tokens", {"demo": "tok_demo"})
    r = client.get(
        "/incidents",
        headers={"X-Tenant-Id": "demo", "Authorization": "Bearer tok_demo"},
    )
    assert r.status_code == 200


def test_public_paths_unaffected_by_bearer_token(client, monkeypatch) -> None:
    """/health, /ready, /metrics must keep working without auth."""
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "tenant_tokens", {"demo": "tok_demo"})
    for path in ("/health", "/ready", "/metrics"):
        r = client.get(path)
        assert r.status_code in (200, 503), f"{path} -> {r.status_code}"


# --- memory-graph isolation ---------------------------------------------


def test_list_incidents_isolates_tenants(client) -> None:
    save_incident(_report_for_tenant("tenant-a", "11111111-1111-1111-1111-111111111111"))
    save_incident(_report_for_tenant("tenant-b", "22222222-2222-2222-2222-222222222222"))
    a_rows = list_incidents(tenant_id="tenant-a", limit=50)
    b_rows = list_incidents(tenant_id="tenant-b", limit=50)
    assert {r.tenant_id for r in a_rows} == {"tenant-a"}
    assert {r.tenant_id for r in b_rows} == {"tenant-b"}


def test_list_root_cause_classes_isolates_tenants(client) -> None:
    """The typed memory graph rows must never cross tenants."""
    save_incident(_report_for_tenant("tenant-a", "33333333-3333-3333-3333-333333333333"))
    save_incident(_report_for_tenant("tenant-b", "44444444-4444-4444-4444-444444444444"))
    a_classes = list_root_cause_classes(tenant_id="tenant-a", service="checkout")
    b_classes = list_root_cause_classes(tenant_id="tenant-b", service="checkout")
    # Each tenant sees its own row (or nothing), but never the other's.
    for row in a_classes:
        # The row carries no tenant_id (it's denormalized), so we verify
        # by counting: each tenant should see at most its own count.
        assert row["last_report_id"].startswith("3"), row
    for row in b_classes:
        assert row["last_report_id"].startswith("4"), row


def test_incident_api_returns_404_for_other_tenants_incident(client) -> None:
    save_incident(_report_for_tenant("tenant-a", "55555555-5555-5555-5555-555555555555"))
    r = client.get(
        "/incidents/55555555-5555-5555-5555-555555555555",
        headers={"X-Tenant-Id": "tenant-b"},
    )
    assert r.status_code == 404
