"""Ops reliability: Retry-After, /metrics, DLQ visibility."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import ai_oncall.jobs.store as jobs_store
from ai_oncall.delivery.send import SlackSendError, _parse_retry_after


def test_parse_retry_after_accepts_int_seconds() -> None:
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("0") == 0.0


def test_parse_retry_after_clamps_negative_to_zero() -> None:
    assert _parse_retry_after("-5") == 0.0


def test_parse_retry_after_returns_none_for_missing_header() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None


def test_slack_send_error_carries_retry_after() -> None:
    e = SlackSendError("rate limited", retry_after_seconds=42)
    assert e.retry_after_seconds == 42


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ONCALL_DISABLE_WORKER", "1")
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    from ai_oncall.server import app

    with TestClient(app) as c:
        yield c


def test_metrics_endpoint_returns_prometheus_format(client) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "ai_oncall_jobs_total" in body
    assert "ai_oncall_jobs_failed_last_15m" in body


def test_jobs_list_filters_by_failed_status(client) -> None:
    from ai_oncall.jobs import enqueue, fail
    from ai_oncall.jobs.store import _conn, claim_next

    # Seed two jobs in the test's isolated DB
    enqueue(kind="rca", tenant_id="demo", idempotency_key="a", payload={"alert_id": "a"})
    enqueue(kind="rca", tenant_id="demo", idempotency_key="b", payload={"alert_id": "b"})
    # Mark one as failed
    j = claim_next()
    assert j is not None
    fail(j.job_id, error="permanent", retry=False)

    r = client.get("/jobs?status=failed", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"


def test_jobs_list_rejects_invalid_status(client) -> None:
    r = client.get("/jobs?status=nonsense", headers={"X-Tenant-Id": "demo"})
    assert r.status_code == 400


def test_fail_with_delay_seconds_overrides_default_backoff(tmp_path, monkeypatch) -> None:
    """When Slack returns Retry-After, fail() schedules the next attempt at
    exactly that delay, not the default exponential backoff."""
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    from datetime import datetime, timezone, timedelta

    from ai_oncall.jobs import enqueue, fail
    from ai_oncall.jobs.store import claim_next

    enqueue(kind="slack_post", tenant_id="demo", idempotency_key="x", payload={"x": 1})
    j = claim_next()
    assert j is not None
    before = datetime.now(timezone.utc)
    j2 = fail(j.job_id, error="429", delay_seconds=20)
    expected = before + timedelta(seconds=20)
    drift = abs((j2.next_attempt_at - expected).total_seconds())
    assert drift < 2.0, f"next_attempt_at drift {drift}s > 2s"
