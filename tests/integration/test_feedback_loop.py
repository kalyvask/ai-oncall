"""Negative-reaction → eval-fixture pipeline.

Round-trips:
- A 👎 reaction -> JSON case file with the original alert + agent's claim.
- Skips reactions whose originating incident is missing.
- Dedupes by (report_id, reaction).
- Honors --tenant-id and --overwrite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_oncall.learnings.incidents as incidents_module
import ai_oncall.learnings.store as learnings_store
from ai_oncall.learnings.feedback_loop import build_case, export_cases, iter_negative_records
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.learnings.store import LearningRecord
from ai_oncall.models import RcaReport

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


@pytest.fixture
def tmp_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    learnings_path = tmp_path / "learnings.jsonl"
    monkeypatch.setattr(learnings_store, "LEARNINGS_PATH", learnings_path)
    return tmp_path


def _append_record(path: Path, record: LearningRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")


# --- iter_negative_records -----------------------------------------------


def test_iter_negative_records_returns_only_negatives(tmp_dbs):
    report = _report()
    save_incident(report)
    learnings_path = learnings_store.LEARNINGS_PATH

    _append_record(learnings_path, LearningRecord(
        tenant_id="demo", report_id=report.report_id, alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="thumbs_up",
    ))
    _append_record(learnings_path, LearningRecord(
        tenant_id="demo", report_id=report.report_id, alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="thumbs_down",
    ))
    _append_record(learnings_path, LearningRecord(
        tenant_id="demo", report_id=report.report_id, alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="wrong_root_cause",
    ))

    records = list(iter_negative_records(learnings_path=learnings_path))
    assert len(records) == 2
    assert {r.reaction for r in records} == {"thumbs_down", "wrong_root_cause"}


def test_iter_negative_records_filters_by_tenant(tmp_dbs):
    learnings_path = learnings_store.LEARNINGS_PATH
    _append_record(learnings_path, LearningRecord(
        tenant_id="A", report_id="r1", alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="thumbs_down",
    ))
    _append_record(learnings_path, LearningRecord(
        tenant_id="B", report_id="r2", alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="thumbs_down",
    ))

    only_a = list(iter_negative_records(learnings_path=learnings_path, tenant_id="A"))
    assert [r.report_id for r in only_a] == ["r1"]


def test_iter_negative_records_skips_malformed_lines(tmp_dbs):
    """Slack action audits in learnings.jsonl are not LearningRecord-shaped;
    they should be silently skipped rather than crashing the export."""
    learnings_path = learnings_store.LEARNINGS_PATH
    learnings_path.parent.mkdir(parents=True, exist_ok=True)
    with learnings_path.open("a", encoding="utf-8") as f:
        f.write('{"kind":"slack_action","report_id":"r1","action_id":"approve_rollback"}\n')
    _append_record(learnings_path, LearningRecord(
        tenant_id="demo", report_id="r2", alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="thumbs_down",
    ))

    records = list(iter_negative_records(learnings_path=learnings_path))
    assert len(records) == 1
    assert records[0].report_id == "r2"


# --- build_case ----------------------------------------------------------


def test_build_case_returns_none_when_incident_missing(tmp_dbs):
    record = LearningRecord(
        tenant_id="demo", report_id="rpt_missing", alert_title="x", service="a",
        top_hypothesis="b", confidence=0.9, reaction="thumbs_down",
    )
    assert build_case(record) is None


def test_build_case_carries_alert_and_wrong_claim(tmp_dbs):
    report = _report()
    save_incident(report)

    record = LearningRecord(
        tenant_id=report.tenant_id, report_id=report.report_id,
        alert_title=report.alert.title, service=report.alert.service,
        top_hypothesis=report.hypotheses[0].root_cause_service,
        confidence=report.hypotheses[0].confidence,
        reaction="wrong_root_cause",
        correction="actually it was the cache, not payments",
    )
    case = build_case(record)
    assert case is not None
    assert case.case_id == f"feedback_{report.report_id}"
    assert case.payload["expected"]["wrong_root_cause_service"] == record.top_hypothesis
    assert case.payload["expected"]["user_label"] == "wrong_root_cause"
    assert "actually it was the cache" in case.payload["expected"]["correction"]
    # The original alert payload is carried verbatim.
    assert case.payload["alert"]["title"] == report.alert.title


# --- export_cases --------------------------------------------------------


def test_export_cases_writes_one_file_per_negative(tmp_dbs):
    report = _report()
    save_incident(report)
    learnings_path = learnings_store.LEARNINGS_PATH

    for reaction in ("thumbs_down", "wrong_root_cause"):
        _append_record(learnings_path, LearningRecord(
            tenant_id=report.tenant_id, report_id=report.report_id,
            alert_title=report.alert.title, service=report.alert.service,
            top_hypothesis=report.hypotheses[0].root_cause_service,
            confidence=report.hypotheses[0].confidence,
            reaction=reaction,
        ))

    out_dir = tmp_dbs / "cases"
    written = export_cases(out_dir, learnings_path=learnings_path)

    assert len(written) == 1  # both reactions share report_id; dedupe to one
    files = sorted(out_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["case_id"] == f"feedback_{report.report_id}"


def test_export_cases_skips_existing_unless_overwrite(tmp_dbs):
    report = _report()
    save_incident(report)
    learnings_path = learnings_store.LEARNINGS_PATH
    _append_record(learnings_path, LearningRecord(
        tenant_id=report.tenant_id, report_id=report.report_id,
        alert_title=report.alert.title, service=report.alert.service,
        top_hypothesis=report.hypotheses[0].root_cause_service,
        confidence=report.hypotheses[0].confidence,
        reaction="thumbs_down",
    ))

    out_dir = tmp_dbs / "cases"
    first = export_cases(out_dir, learnings_path=learnings_path)
    assert len(first) == 1

    second = export_cases(out_dir, learnings_path=learnings_path)
    assert len(second) == 0  # already exists, no overwrite

    third = export_cases(out_dir, learnings_path=learnings_path, overwrite=True)
    assert len(third) == 1
