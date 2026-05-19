"""Action policy enforcement + dry-run approve endpoint.

Two surfaces:

1. ``stage_actions`` must downgrade any propose/auto tier whose kind is
   not on the allowlist. Even if the LLM hand-supplies an unsafe
   ``StagedAction``, the policy pass overrides it.

2. ``POST /actions/{id}/approve`` with ``dry_run=true`` returns the action
   preview without dispatching. The audit log is NOT written for dry runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ai_oncall.jobs.store as jobs_store
import ai_oncall.learnings.incidents as incidents_module
import ai_oncall.learnings.store as learnings_store
from ai_oncall.agent.policy import DEFAULT_POLICY, downgrade_unsafe_tier
from ai_oncall.agent.staging import stage_actions
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.models import RcaReport, StagedAction

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    return RcaReport.model_validate_json(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )


# --- policy --------------------------------------------------------------


def test_downgrade_unsafe_tier_blocks_propose_for_manual_kind() -> None:
    new_tier, reason = downgrade_unsafe_tier("manual", "propose")
    assert new_tier == "recommend"
    assert "not on the propose whitelist" in (reason or "")


def test_downgrade_unsafe_tier_blocks_auto_for_restart() -> None:
    new_tier, reason = downgrade_unsafe_tier("restart", "auto")
    assert new_tier == "propose"
    assert reason is not None


def test_downgrade_unsafe_tier_passes_allowed_combinations() -> None:
    new_tier, reason = downgrade_unsafe_tier("rollback", "propose")
    assert new_tier == "propose"
    assert reason is None


def test_stage_actions_downgrades_llm_supplied_unsafe_tier() -> None:
    """Even if the LLM ships back a StagedAction with tier=auto on a kind
    that isn't on the auto whitelist, the policy pass downgrades it."""
    report = _report()
    h = report.hypotheses[0]
    unsafe = StagedAction(kind="manual", service=h.root_cause_service, tier="propose")
    new_h = h.model_copy(update={"staged_action": unsafe})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})

    out = stage_actions(report)
    assert out.hypotheses[0].staged_action.tier == "recommend"
    assert "Policy override" in (out.hypotheses[0].staged_action.rationale or "")


def test_blast_radius_for_kinds() -> None:
    assert DEFAULT_POLICY.blast_radius_for("rollback") == "medium"
    assert DEFAULT_POLICY.blast_radius_for("feature_flag") == "low"
    assert DEFAULT_POLICY.blast_radius_for("manual") == "high"


# --- dry-run approve -----------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ONCALL_DISABLE_WORKER", "1")
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    monkeypatch.setattr(learnings_store, "LEARNINGS_PATH", tmp_path / "learnings.jsonl")
    from ai_oncall.server import app

    with TestClient(app) as c:
        yield c


def _save_propose_report() -> RcaReport:
    report = _report()
    h = report.hypotheses[0]
    sa = StagedAction(kind="rollback", service=h.root_cause_service, tier="propose")
    new_h = h.model_copy(update={"staged_action": sa})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    save_incident(report)
    return report


def test_approve_dry_run_returns_preview_without_dispatching(client) -> None:
    report = _save_propose_report()
    r = client.post(
        f"/actions/{report.report_id}/approve",
        json={"dry_run": True, "user_id": "U1", "user_name": "alex"},
        headers={"X-Tenant-Id": "demo"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["preview"]["kind"] == "rollback"
    assert body["preview"]["blast_radius"] == "medium"
    # No audit row should have been written for a dry run.
    audit_path = learnings_store.LEARNINGS_PATH
    if audit_path.exists():
        text = audit_path.read_text(encoding="utf-8")
        assert "slack_action" not in text  # no real audit rows


def test_approve_real_call_writes_audit_with_blast_radius(client) -> None:
    report = _save_propose_report()
    r = client.post(
        f"/actions/{report.report_id}/approve",
        json={"user_id": "U1", "user_name": "alex"},
        headers={"X-Tenant-Id": "demo"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "preview" in body
    audit_path = learnings_store.LEARNINGS_PATH
    assert audit_path.exists()
    text = audit_path.read_text(encoding="utf-8")
    audit_lines = [json.loads(line) for line in text.splitlines() if line.strip() and '"kind": "slack_action"' in line]
    assert audit_lines, "expected an audit row from approve"
    assert "blast=medium" in audit_lines[-1]["detail"]


def test_approve_rejects_recommend_tier(client) -> None:
    report = _report()
    h = report.hypotheses[0]
    sa = StagedAction(kind="rollback", service=h.root_cause_service, tier="recommend")
    new_h = h.model_copy(update={"staged_action": sa})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    save_incident(report)
    r = client.post(
        f"/actions/{report.report_id}/approve",
        json={},
        headers={"X-Tenant-Id": "demo"},
    )
    assert r.status_code == 403
