"""Slack interactivity: signature verification, action routing, audit log.

The whole flow is tested without an actual Slack workspace: we synthesize
a signed request body, pass it through ``verify_slack_signature``, then
run ``handle_interaction`` against an in-memory incidents DB.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import pytest

import ai_oncall.learnings.incidents as incidents_module
import ai_oncall.learnings.store as learnings_store
from ai_oncall.delivery.cd_dispatch import sign_dispatch_body
from ai_oncall.delivery.reactions import (
    handle_interaction,
    parse_interaction_payload,
    verify_slack_signature,
)
from ai_oncall.delivery.slack import render_parent
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.models import RcaReport, StagedAction

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


@pytest.fixture
def tmp_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    monkeypatch.setattr(learnings_store, "LEARNINGS_PATH", tmp_path / "learnings.jsonl")
    return tmp_path


# --- signature verification --------------------------------------------


def test_verify_slack_signature_accepts_correctly_signed_request() -> None:
    secret = "test-secret"
    body = b'payload={"some":"json"}'
    ts = str(int(time.time()))
    base = f"v0:{ts}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    sig = f"v0={digest}"

    assert verify_slack_signature(
        signing_secret=secret, request_body=body, timestamp=ts, signature=sig
    )


def test_verify_slack_signature_rejects_wrong_signature() -> None:
    assert not verify_slack_signature(
        signing_secret="test-secret",
        request_body=b"payload={}",
        timestamp=str(int(time.time())),
        signature="v0=wrong",
    )


def test_verify_slack_signature_rejects_old_timestamp() -> None:
    secret = "test-secret"
    body = b"payload={}"
    ts = str(int(time.time()) - 10 * 60)  # 10 minutes old
    base = f"v0:{ts}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()

    assert not verify_slack_signature(
        signing_secret=secret,
        request_body=body,
        timestamp=ts,
        signature=f"v0={digest}",
    )


def test_verify_slack_signature_refuses_when_secret_unset() -> None:
    """Empty secret means we can't verify; refuse rather than silently allow."""
    assert not verify_slack_signature(
        signing_secret="",
        request_body=b"x",
        timestamp=str(int(time.time())),
        signature="v0=anything",
    )


# --- payload parsing ---------------------------------------------------


def test_parse_interaction_payload_decodes_form_encoded_json() -> None:
    inner = {"actions": [{"action_id": "approve_rollback", "value": "rpt_x"}]}
    body = urllib.parse.urlencode({"payload": json.dumps(inner)}).encode("utf-8")

    parsed = parse_interaction_payload(body)
    assert parsed["actions"][0]["action_id"] == "approve_rollback"


# --- action routing ---------------------------------------------------


def _propose_report(tmp_dbs) -> RcaReport:
    """Save a report whose top hypothesis has staged_action.tier == 'propose'."""
    report = _report()
    # Force tier=propose so the rollback button is gated correctly.
    h = report.hypotheses[0]
    new_action = (
        h.staged_action
        or StagedAction(kind="rollback", service=h.root_cause_service, tier="propose")
    ).model_copy(update={"tier": "propose", "kind": "rollback"})
    new_h = h.model_copy(update={"staged_action": new_action})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    save_incident(report)
    return report


def test_approve_rollback_records_outcome_when_dry_run(tmp_dbs) -> None:
    """No CD URL configured -> dispatch is a dry-run; outcome.success=False
    with a clear detail message; the action is still audited."""
    report = _propose_report(tmp_dbs)
    payload = {
        "user": {"id": "U1", "name": "alex"},
        "actions": [{"action_id": "approve_rollback", "value": report.report_id}],
    }
    outcomes = handle_interaction(payload, cd_dispatch_url=None)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.action_id == "approve_rollback"
    assert o.report_id == report.report_id
    assert o.success is False
    assert "dry-run" in o.detail.lower()


def test_approve_rollback_writes_audit_row(tmp_dbs) -> None:
    """Every approve_rollback handling must persist an audit row to
    learnings.jsonl with kind=slack_action. Silent audit failures defeat
    the entire purpose of an audit log."""
    report = _propose_report(tmp_dbs)
    payload = {
        "user": {"id": "U7", "name": "auditor"},
        "actions": [{"action_id": "approve_rollback", "value": report.report_id}],
    }
    handle_interaction(payload, cd_dispatch_url=None)

    learnings_path = learnings_store.LEARNINGS_PATH
    assert learnings_path.exists(), "audit log was never written"
    lines = [
        line for line in learnings_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    audit_rows = [
        json.loads(line)
        for line in lines
        if '"kind": "slack_action"' in line or '"kind":"slack_action"' in line
    ]
    assert audit_rows, "no slack_action audit row found"
    row = audit_rows[-1]
    assert row["action_id"] == "approve_rollback"
    assert row["report_id"] == report.report_id
    assert row["user_id"] == "U7"
    assert row["user_name"] == "auditor"
    assert "at" in row


def test_approve_rollback_refuses_when_action_tier_is_recommend(tmp_dbs) -> None:
    """The button only fires for `propose` (one-click). `recommend` should
    refuse."""
    report = _report()
    h = report.hypotheses[0]
    new_action = StagedAction(kind="rollback", service=h.root_cause_service, tier="recommend")
    new_h = h.model_copy(update={"staged_action": new_action})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    save_incident(report)

    payload = {
        "user": {"id": "U1", "name": "alex"},
        "actions": [{"action_id": "approve_rollback", "value": report.report_id}],
    }
    outcomes = handle_interaction(payload)
    assert outcomes[0].success is False
    assert "recommend" in outcomes[0].detail


def test_mark_wrong_root_cause_appends_learning_record(tmp_dbs) -> None:
    report = _propose_report(tmp_dbs)
    payload = {
        "user": {"id": "U1", "name": "alex"},
        "actions": [{"action_id": "mark_wrong_root_cause", "value": report.report_id}],
    }
    outcomes = handle_interaction(payload)
    assert outcomes[0].success is True

    learnings_path = learnings_store.LEARNINGS_PATH
    text = learnings_path.read_text(encoding="utf-8")
    assert "wrong_root_cause" in text


def test_unknown_action_id_returns_failure(tmp_dbs) -> None:
    report = _propose_report(tmp_dbs)
    payload = {
        "user": {"id": "U1", "name": "alex"},
        "actions": [{"action_id": "nope", "value": report.report_id}],
    }
    outcomes = handle_interaction(payload)
    assert outcomes[0].success is False


# --- Slack renderer ----------------------------------------------------


def test_slack_parent_renders_action_buttons_when_propose(tmp_dbs) -> None:
    """The Block Kit renderer must include an `actions` block when the
    top hypothesis is staged as `propose`. This is the visible cupcake."""
    report = _propose_report(tmp_dbs)
    blocks = render_parent(report)
    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert action_blocks, "propose-tier hypothesis must render action buttons"
    # The rollback button carries report_id in `value` so the dispatcher
    # can route. Confirm the button exists with a confirm dialog.
    elements = action_blocks[0]["elements"]
    rollback_btns = [e for e in elements if e.get("action_id") == "approve_rollback"]
    assert rollback_btns
    assert rollback_btns[0]["value"] == report.report_id
    assert "confirm" in rollback_btns[0]


def test_slack_parent_omits_action_buttons_when_recommend(tmp_dbs) -> None:
    """A `recommend` tier should not get one-click buttons."""
    report = _report()
    h = report.hypotheses[0]
    new_action = StagedAction(kind="rollback", service=h.root_cause_service, tier="recommend")
    new_h = h.model_copy(update={"staged_action": new_action})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})

    blocks = render_parent(report)
    assert not any(b.get("type") == "actions" for b in blocks)


# --- cd_dispatch sign helper ------------------------------------------


def test_sign_dispatch_body_is_deterministic() -> None:
    body = b'{"x":1}'
    secret = "rotating-secret"
    a = sign_dispatch_body(body, secret)
    b = sign_dispatch_body(body, secret)
    assert a == b
    assert len(a) == 64  # sha256 hex
