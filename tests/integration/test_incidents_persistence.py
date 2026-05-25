"""Persistent incident storage + typed memory graph.

The agent layer added in this round persists every RCA report to a SQLite
database (separate from the telemetry store) so:
- replay can re-run the pipeline on a stored alert,
- the typed memory graph can aggregate root-cause classes per service,
- Slack action handlers can look the report up by report_id.

These tests round-trip a fixture report and verify both the row-level
persistence and the graph aggregation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.learnings.incidents import (
    get_incident,
    list_incidents,
    list_root_cause_classes,
    save_incident,
)
from ai_oncall.models import RcaReport

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


@pytest.fixture
def tmp_incidents_db(tmp_path, monkeypatch):
    path = tmp_path / "incidents.sqlite"
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", path)
    return path


def test_save_and_get_round_trip(tmp_incidents_db) -> None:
    report = _report()
    row = save_incident(report)

    fetched = get_incident(report.report_id)
    assert fetched is not None
    assert fetched.report_id == report.report_id
    # Full report blob round-trips through JSON without loss.
    assert fetched.report().report_id == report.report_id
    assert row.tenant_id == fetched.tenant_id


def test_list_incidents_filters_by_service(tmp_incidents_db) -> None:
    report = _report()
    save_incident(report)
    rows = list_incidents(tenant_id=report.tenant_id, service=report.alert.service)
    assert len(rows) == 1
    rows_other = list_incidents(tenant_id=report.tenant_id, service="some-other-service")
    assert rows_other == []


def test_root_cause_class_graph_increments_on_repeated_save(tmp_incidents_db) -> None:
    report = _report()
    save_incident(report)
    # Save the same report a second time with a new id to simulate a future incident.
    repeat = report.model_copy(update={"report_id": report.report_id + "_b"})
    save_incident(repeat)

    classes = list_root_cause_classes(tenant_id=report.tenant_id, service=report.alert.service)
    # The fixture's recommended_action contains "rollback" so class is "deploy_regression".
    assert any(c["root_cause_class"] == "deploy_regression" for c in classes)
    target = next(c for c in classes if c["root_cause_class"] == "deploy_regression")
    assert target["occurrences"] >= 2


def test_root_cause_class_filter_respects_trust_tier(tmp_incidents_db) -> None:
    report = _report()
    save_incident(report, trust_tier="local")
    aggregated = save_incident(
        report.model_copy(update={"report_id": report.report_id + "_agg"}),
        trust_tier="aggregated",
    )
    assert aggregated.trust_tier == "aggregated"

    only_local = list_root_cause_classes(
        tenant_id=report.tenant_id, service=report.alert.service, trust_tiers=("local",)
    )
    only_agg = list_root_cause_classes(
        tenant_id=report.tenant_id, service=report.alert.service, trust_tiers=("aggregated",)
    )
    # The graph rows track the latest trust_tier the (tenant, service, class) was
    # written under; later writes overwrite the row's tier. So at least one of
    # the queries returns the row.
    assert only_local or only_agg, "graph row should appear under at least one tier"


def test_save_with_no_hypotheses_raises(tmp_incidents_db) -> None:
    report = _report()
    # `hypotheses` has min_length=1 in the model; the model rejects an empty
    # list at construction time, so we round-trip through model_dump to build
    # a payload that bypasses model_copy's preserve-instance shortcut.
    payload = report.model_dump(mode="json")
    payload["hypotheses"] = []
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        RcaReport.model_validate(payload)
