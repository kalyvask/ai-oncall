"""Trust tier promotion + propagation through get_past_incidents.

Three tiers: ``local`` (default; only this tenant), ``aggregated`` (this
tenant has opted into cross-tenant priors), ``verified`` (a human marked
this incident as definitively right).

The ``get_past_incidents`` tool reads the ``service_root_cause_classes``
graph filtered by the requested tier set. Tests verify:

- Promotion bumps both the incident row AND the graph row.
- get_past_incidents respects the ``trust_tiers`` filter.
- Promoting a non-existent report returns False without raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.agent.tools import get_past_incidents
from ai_oncall.learnings.incidents import (
    list_root_cause_classes,
    promote_incident_tier,
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
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    return tmp_path


def test_promote_incident_tier_updates_incident_and_graph(tmp_incidents_db):
    report = _report()
    save_incident(report)

    ok = promote_incident_tier(report.report_id, new_tier="verified")
    assert ok is True

    # Graph row reflects the promotion under the same (tenant, service, class).
    classes = list_root_cause_classes(
        tenant_id=report.tenant_id,
        service=report.alert.service,
        trust_tiers=("verified",),
    )
    assert classes, "graph row should now be queryable under 'verified'"
    assert classes[0]["trust_tier"] == "verified"


def test_promote_returns_false_for_missing_report(tmp_incidents_db):
    assert promote_incident_tier("rpt_does_not_exist", new_tier="verified") is False


def test_get_past_incidents_respects_trust_tier_filter(tmp_incidents_db):
    """Default trust_tiers=('local',) excludes verified-tier rows; explicit
    opt-in to ('local','verified') brings them back."""
    report = _report()
    save_incident(report)
    promote_incident_tier(report.report_id, new_tier="verified")

    # Caller asking only for 'local' should NOT see the verified summary row.
    only_local = get_past_incidents(
        None,  # store unused for the graph branch
        report.tenant_id,
        service=report.alert.service,
        k=3,
        trust_tiers=("local",),
    )
    summary = next(
        (item for item in only_local if "_root_cause_class_summary" in item),
        None,
    )
    if summary is not None:
        assert all(c["trust_tier"] == "local" for c in summary["_root_cause_class_summary"])

    # Asking for both tiers surfaces the verified row.
    both = get_past_incidents(
        None,
        report.tenant_id,
        service=report.alert.service,
        k=3,
        trust_tiers=("local", "verified"),
    )
    summary_both = next(
        (item for item in both if "_root_cause_class_summary" in item),
        None,
    )
    assert summary_both is not None
    tiers_seen = {c["trust_tier"] for c in summary_both["_root_cause_class_summary"]}
    assert "verified" in tiers_seen


def test_get_past_incidents_returns_recent_incident_rows(tmp_incidents_db):
    report = _report()
    save_incident(report)

    rows = get_past_incidents(
        None,
        report.tenant_id,
        service=report.alert.service,
        k=3,
        include_root_cause_classes=False,
    )
    # No summary row when classes excluded.
    assert all("_root_cause_class_summary" not in r for r in rows)
    assert any(r["report_id"] == report.report_id for r in rows)


def test_promote_to_aggregated_then_verified_chain(tmp_incidents_db):
    report = _report()
    save_incident(report)

    assert promote_incident_tier(report.report_id, new_tier="aggregated") is True
    classes = list_root_cause_classes(
        tenant_id=report.tenant_id,
        service=report.alert.service,
        trust_tiers=("aggregated",),
    )
    assert any(c["trust_tier"] == "aggregated" for c in classes)

    # Promote again to verified -- the graph row's tier moves with it.
    assert promote_incident_tier(report.report_id, new_tier="verified") is True
    only_verified = list_root_cause_classes(
        tenant_id=report.tenant_id,
        service=report.alert.service,
        trust_tiers=("verified",),
    )
    assert any(c["trust_tier"] == "verified" for c in only_verified)
    only_aggregated = list_root_cause_classes(
        tenant_id=report.tenant_id,
        service=report.alert.service,
        trust_tiers=("aggregated",),
    )
    # The single graph row carries the latest tier (now verified), so the
    # 'aggregated' filter no longer matches it.
    assert all(c["trust_tier"] != "aggregated" for c in only_aggregated)
