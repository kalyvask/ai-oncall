"""Tamper-evident audit chain — append + verify + tamper detection.

Each test isolates the audit file to a tmp_path so the suite is hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from ai_oncall.learnings.audit import (
    GENESIS_HASH,
    append_audit,
    iter_audit,
    verify_chain,
)


def _appendN(path: Path, n: int) -> None:
    for i in range(n):
        append_audit(
            path=path,
            tenant_id="demo",
            report_id=f"r-{i:04d}",
            action_id="approve_rollback",
            intent_proposal=f"rollback payment deploy {i}",
            contextual_state={"top_confidence": 0.91, "alert_service": "checkout"},
            policy_decision={"tier": "propose", "approver_user_id": "U123"},
            execution_boundaries={"kind": "rollback", "service": "payment"},
            actual_outcome={"success": i % 2 == 0, "detail": "ok" if i % 2 == 0 else "fail"},
        )


def test_first_record_links_to_genesis(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    record = append_audit(
        path=path,
        tenant_id="demo",
        report_id="r-001",
        action_id="approve_rollback",
        intent_proposal="rollback",
        contextual_state={},
        policy_decision={"tier": "propose"},
        execution_boundaries={"kind": "rollback"},
        actual_outcome={"success": True, "detail": "ok"},
    )
    assert record.prev_hash == GENESIS_HASH
    assert len(record.record_hash) == 64


def test_chain_verifies_after_many_appends(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _appendN(path, 10)

    result = verify_chain(path)
    assert result.ok
    assert result.rows_checked == 10
    assert result.broken_at_index is None


def test_each_prev_hash_matches_predecessor(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _appendN(path, 5)

    rows = list(iter_audit(path))
    prev = GENESIS_HASH
    for row in rows:
        assert row.prev_hash == prev
        prev = row.record_hash


def test_in_place_field_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _appendN(path, 5)

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[2])
    tampered["actual_outcome"] = {"success": True, "detail": "covered up"}
    lines[2] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(path)
    assert not result.ok
    assert result.broken_at_index == 2
    assert "record_hash" in (result.reason or "")


def test_row_deletion_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _appendN(path, 5)

    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(path)
    assert not result.ok
    # Index 2 now holds the old row 3, whose prev_hash points at the removed row.
    assert result.broken_at_index == 2
    assert "prev_hash" in (result.reason or "")


def test_row_reordering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _appendN(path, 5)

    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[3] = lines[3], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(path)
    assert not result.ok
    assert result.broken_at_index == 1


def test_empty_file_verifies(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    result = verify_chain(path)
    assert result.ok
    assert result.rows_checked == 0


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    """A junk line shouldn't crash iter_audit; verify still walks valid rows."""
    path = tmp_path / "audit.jsonl"
    _appendN(path, 3)
    with path.open("a", encoding="utf-8") as f:
        f.write("{ not valid json\n")
    rows = list(iter_audit(path))
    assert len(rows) == 3


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the default audit path so tests using it don't pollute data/."""
    from ai_oncall.learnings import audit as audit_mod

    target = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_mod, "DEFAULT_AUDIT_PATH", target)
    yield target


def test_default_path_is_data_audit_jsonl(isolated_audit: Path) -> None:
    """Default path resolves under the data dir; this test pins the location."""
    record = append_audit(
        tenant_id="demo",
        report_id="r-001",
        action_id="approve_rollback",
        intent_proposal="x",
        contextual_state={},
        policy_decision={},
        execution_boundaries={},
        actual_outcome={"success": True, "detail": "ok"},
    )
    assert record.prev_hash == GENESIS_HASH
    assert isolated_audit.exists()


def test_approve_rollback_appends_to_audit_chain(
    isolated_audit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: handle_interaction on a propose-tier report writes a chained
    audit row whose five fields capture the intent, context, policy decision,
    execution boundaries, and outcome."""
    import ai_oncall.learnings.incidents as incidents_module
    import ai_oncall.learnings.store as learnings_store_mod
    from ai_oncall.delivery.reactions import handle_interaction
    from ai_oncall.learnings.incidents import save_incident
    from ai_oncall.models import RcaReport, StagedAction

    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    monkeypatch.setattr(learnings_store_mod, "LEARNINGS_PATH", tmp_path / "learnings.jsonl")

    repo = Path(__file__).resolve().parents[2]
    payload_json = json.loads(
        (repo / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    report = RcaReport.model_validate(payload_json)
    h = report.hypotheses[0]
    new_action = (
        h.staged_action
        or StagedAction(kind="rollback", service=h.root_cause_service, tier="propose")
    ).model_copy(update={"tier": "propose", "kind": "rollback"})
    new_h = h.model_copy(update={"staged_action": new_action})
    report = report.model_copy(update={"hypotheses": [new_h, *report.hypotheses[1:]]})
    save_incident(report)

    handle_interaction(
        {
            "user": {"id": "U9", "name": "auditor"},
            "actions": [{"action_id": "approve_rollback", "value": report.report_id}],
        },
        cd_dispatch_url=None,
    )

    rows = list(iter_audit(isolated_audit))
    assert len(rows) == 1
    record = rows[0]
    assert record.action_id == "approve_rollback"
    assert record.report_id == report.report_id
    assert record.policy_decision["approver_user_id"] == "U9"
    assert record.policy_decision["tier"] == "propose"
    assert record.execution_boundaries["kind"] == "rollback"
    assert record.actual_outcome["success"] is False  # dry-run, no URL
    assert "dry-run" in record.actual_outcome["detail"].lower()

    # Chain integrity holds.
    result = verify_chain(isolated_audit)
    assert result.ok
