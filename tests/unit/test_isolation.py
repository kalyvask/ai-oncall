"""Store-layer tenant isolation.

``get_incident`` and ``get_job`` enforce tenant isolation themselves when a
``tenant_id`` is passed: a row owned by another tenant is indistinguishable
from "not found". This is the boundary the HTTP routes rely on, so it's worth
locking in at the store level rather than only through the API. Omitting
``tenant_id`` is the documented escape hatch for the Slack event path and must
keep returning the row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_oncall.jobs.store as jobs_store
import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.jobs.store import enqueue, get_job
from ai_oncall.learnings.incidents import get_incident, save_incident
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


@pytest.fixture
def tmp_jobs_db(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")


def test_get_incident_hides_other_tenants_row(tmp_incidents_db) -> None:
    report = _report()
    owner = report.tenant_id
    save_incident(report)

    # Wrong tenant: indistinguishable from not-found.
    assert get_incident(report.report_id, tenant_id="someone-else") is None
    # Right tenant: returned.
    mine = get_incident(report.report_id, tenant_id=owner)
    assert mine is not None and mine.report_id == report.report_id
    # No tenant filter (Slack event path): still returned.
    assert get_incident(report.report_id) is not None


def test_get_job_hides_other_tenants_job(tmp_jobs_db) -> None:
    job = enqueue(
        kind="rca",
        tenant_id="acme",
        idempotency_key="alert-1",
        payload={"hello": "world"},
    )

    assert get_job(job.job_id, tenant_id="evil-corp") is None
    mine = get_job(job.job_id, tenant_id="acme")
    assert mine is not None and mine.job_id == job.job_id
    # No tenant filter: still returned.
    assert get_job(job.job_id) is not None
