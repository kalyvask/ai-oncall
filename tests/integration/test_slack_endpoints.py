"""End-to-end tests for the FastAPI Slack endpoints.

These exercise:
- ``POST /webhooks/slack/action`` — signature verification + action dispatch.
- ``POST /webhooks/slack/event`` — URL verification handshake + thread Q&A
  resolution from the parent message context.
- The signed-secret guardrail (signing_secret missing -> 401).

The handlers reach into the persisted incidents store, so each test gets a
fresh tmp DB via the ``tmp_dbs`` fixture.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_oncall.learnings.incidents as incidents_module
import ai_oncall.learnings.store as learnings_store
import ai_oncall.settings as settings_module
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.models import RcaReport, StagedAction
from ai_oncall.server import _resolve_report_id_from_event, app

REPO = Path(__file__).resolve().parents[2]
SIGNING_SECRET = "test-signing-secret"


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


@pytest.fixture
def tmp_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    monkeypatch.setattr(learnings_store, "LEARNINGS_PATH", tmp_path / "learnings.jsonl")
    monkeypatch.setattr(settings_module.settings, "slack_signing_secret", SIGNING_SECRET)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


def _sign(body: bytes, *, secret: str = SIGNING_SECRET, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    base = f"v0:{ts}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _propose_report(tmp_dbs) -> RcaReport:
    report = _report()
    h = report.hypotheses[0]
    new_action = StagedAction(kind="rollback", service=h.root_cause_service, tier="propose")
    new_h = h.model_copy(update={"staged_action": new_action})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    save_incident(report)
    return report


# --- /webhooks/slack/action --------------------------------------------


def test_slack_action_returns_401_on_bad_signature(tmp_dbs, client):
    body = urllib.parse.urlencode({"payload": json.dumps({})}).encode("utf-8")
    bad_headers = _sign(body, secret="wrong-secret")
    bad_headers["Content-Type"] = "application/x-www-form-urlencoded"
    resp = client.post("/webhooks/slack/action", content=body, headers=bad_headers)
    assert resp.status_code == 401


def test_slack_action_returns_401_when_signing_secret_unset(tmp_dbs, client, monkeypatch):
    """Empty secret -> verify_slack_signature refuses by design."""
    monkeypatch.setattr(settings_module.settings, "slack_signing_secret", None)
    body = urllib.parse.urlencode({"payload": json.dumps({})}).encode("utf-8")
    headers = _sign(body, secret="anything")
    resp = client.post("/webhooks/slack/action", content=body, headers=headers)
    assert resp.status_code == 401


def test_slack_action_dispatches_approve_rollback_dry_run(tmp_dbs, client):
    report = _propose_report(tmp_dbs)
    payload = {
        "user": {"id": "U1", "name": "alex"},
        "actions": [{"action_id": "approve_rollback", "value": report.report_id}],
    }
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    resp = client.post("/webhooks/slack/action", content=body, headers=_sign(body))
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcomes"][0]["action_id"] == "approve_rollback"
    assert data["outcomes"][0]["report_id"] == report.report_id
    # Without cd_dispatch_url configured, dispatch is a dry-run.
    assert data["outcomes"][0]["success"] is False
    assert "dry-run" in data["outcomes"][0]["detail"].lower()


def test_slack_action_rejects_old_timestamp(tmp_dbs, client):
    """Replay protection: payloads older than 5 minutes are refused."""
    report = _propose_report(tmp_dbs)
    payload = {
        "user": {"id": "U1", "name": "alex"},
        "actions": [{"action_id": "approve_rollback", "value": report.report_id}],
    }
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    old_ts = str(int(time.time()) - 10 * 60)
    headers = _sign(body, ts=old_ts)
    resp = client.post("/webhooks/slack/action", content=body, headers=headers)
    assert resp.status_code == 401


# --- /webhooks/slack/event ---------------------------------------------


def test_slack_event_url_verification_handshake(tmp_dbs, client):
    payload = {"type": "url_verification", "challenge": "asdf1234"}
    body = json.dumps(payload).encode("utf-8")
    headers = _sign(body)
    headers["Content-Type"] = "application/json"
    resp = client.post("/webhooks/slack/event", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "asdf1234"}


def test_slack_event_ignores_non_thread_messages(tmp_dbs, client):
    payload = {
        "type": "event_callback",
        "event": {"type": "message", "text": "hi", "ts": "1.0"},  # no thread_ts
    }
    body = json.dumps(payload).encode("utf-8")
    headers = _sign(body)
    headers["Content-Type"] = "application/json"
    resp = client.post("/webhooks/slack/event", content=body, headers=headers)
    assert resp.status_code == 200
    assert "ignored" in resp.json()


def test_slack_event_returns_401_on_bad_signature(tmp_dbs, client):
    body = json.dumps({"type": "event_callback"}).encode("utf-8")
    headers = _sign(body, secret="wrong-secret")
    headers["Content-Type"] = "application/json"
    resp = client.post("/webhooks/slack/event", content=body, headers=headers)
    assert resp.status_code == 401


# --- _resolve_report_id_from_event --------------------------------------


def test_resolve_report_id_finds_uuid_in_message_blocks():
    event = {
        "type": "message",
        "text": "why redis?",
        "thread_ts": "1.0",
        "ts": "2.0",
        "message": {
            "text": "🚨 checkout p99 spike",
            "blocks": [
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "id `0193f4a4-2b87-7a31-9c1f-1d6a93dca8c1`"}
                    ],
                }
            ],
        },
    }
    rid = _resolve_report_id_from_event(event)
    assert rid == "0193f4a4-2b87-7a31-9c1f-1d6a93dca8c1"


def test_resolve_report_id_finds_rpt_token():
    event = {"text": "follow up on report rpt_abc-123 please", "message": {}}
    assert _resolve_report_id_from_event(event) == "rpt_abc-123"


def test_resolve_report_id_returns_none_when_no_id():
    event = {"text": "hello", "message": {"text": "no id here"}}
    assert _resolve_report_id_from_event(event) is None


# --- settings warnings -------------------------------------------------


def test_warn_unsafe_settings_flags_cd_url_without_secret(monkeypatch):
    monkeypatch.setattr(settings_module.settings, "cd_dispatch_url", "https://x")
    monkeypatch.setattr(settings_module.settings, "cd_dispatch_secret", None)
    monkeypatch.setattr(settings_module.settings, "slack_signing_secret", "x")
    warnings = settings_module.warn_unsafe_settings()
    assert any("CD_DISPATCH_SECRET" in w for w in warnings)


def test_warn_unsafe_settings_flags_missing_slack_secret(monkeypatch):
    monkeypatch.setattr(settings_module.settings, "cd_dispatch_url", None)
    monkeypatch.setattr(settings_module.settings, "slack_signing_secret", None)
    warnings = settings_module.warn_unsafe_settings()
    assert any("SLACK_SIGNING_SECRET" in w for w in warnings)


def test_warn_unsafe_settings_silent_when_configured(monkeypatch):
    monkeypatch.setattr(settings_module.settings, "cd_dispatch_url", None)
    monkeypatch.setattr(settings_module.settings, "slack_signing_secret", "x")
    assert settings_module.warn_unsafe_settings() == []
