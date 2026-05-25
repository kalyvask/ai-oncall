"""Durable alert→RCA→Slack job pipeline.

The webhook enqueues an ``rca`` job; the worker runs it; on success a
``slack_post`` job is queued. Retries fire with exponential backoff. All of
this is driven synchronously in tests via ``process_one`` so we don't need
the asyncio loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_oncall.jobs.store as jobs_store
import ai_oncall.jobs.worker as jobs_worker
import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.agent.prompts import plan_v1, synthesize_v1
from ai_oncall.jobs import enqueue, get_job, process_one
from ai_oncall.jobs.store import claim_next, fail
from ai_oncall.llm.client import MockLlm

REPO = Path(__file__).resolve().parents[2]


def _alert_payload() -> dict:
    return json.loads((REPO / "fixtures/synthetic_alerts/checkout_regression.json").read_text())


def _seeded_llm() -> MockLlm:
    """LLM seeded with fixture responses for PLAN and SYNTHESIZE prompts."""
    alert = _alert_payload()
    expected = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    plan_payload = {
        "tenant_id": alert["tenant_id"],
        "alert_id": alert["alert_id"],
        "hypotheses": [
            {
                "statement": "payment regression",
                "confidence": 0.7,
                "queries": [
                    {"tool": "get_topology", "input": {"service": "checkout", "depth": 2}},
                    {
                        "tool": "get_recent_deploys",
                        "input": {"service": "payment", "since": "2026-04-24T03:14:00Z"},
                    },
                ],
            },
            {
                "statement": "checkout self",
                "confidence": 0.2,
                "queries": [
                    {
                        "tool": "get_recent_deploys",
                        "input": {"service": "checkout", "since": "2026-04-24T03:14:00Z"},
                    },
                ],
            },
            {
                "statement": "external stripe",
                "confidence": 0.1,
                "queries": [
                    {"tool": "get_runbook", "input": {"service": "payment"}},
                ],
            },
        ],
    }
    return MockLlm(
        fixtures={
            plan_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(plan_payload),
                "tokens_in": 800,
                "tokens_out": 200,
            },
            synthesize_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(expected),
                "tokens_in": 4000,
                "tokens_out": 600,
            },
        }
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_DB_PATH", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(incidents_module, "INCIDENTS_DB_PATH", tmp_path / "incidents.sqlite")
    monkeypatch.setattr(jobs_worker, "get_llm_client", _seeded_llm)
    return tmp_path


def test_enqueue_returns_pending_job(isolated) -> None:
    payload = _alert_payload()
    job = enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key=payload["alert_id"],
        payload={**payload, "tenant_id": "demo"},
    )
    assert job.status == "pending"
    assert job.kind == "rca"
    assert job.attempts == 0


def test_duplicate_enqueue_returns_existing_job(isolated) -> None:
    payload = _alert_payload()
    j1 = enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key=payload["alert_id"],
        payload={**payload, "tenant_id": "demo"},
    )
    j2 = enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key=payload["alert_id"],
        payload={**payload, "tenant_id": "demo"},
    )
    assert j1.job_id == j2.job_id


def test_worker_runs_rca_and_writes_incident(isolated) -> None:
    payload = _alert_payload()
    job = enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key=payload["alert_id"],
        payload={**payload, "tenant_id": "demo"},
    )
    processed = process_one()
    assert processed is not None
    final = get_job(job.job_id)
    assert final is not None
    assert final.status == "done"
    result = final.result()
    assert result is not None
    assert "report_id" in result


def test_worker_skips_slack_post_when_no_token(isolated, monkeypatch) -> None:
    """Without slack creds, the worker should NOT enqueue a slack_post."""
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "slack_bot_token", None)
    monkeypatch.setattr(settings, "slack_default_channel", None)
    payload = _alert_payload()
    enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key=payload["alert_id"],
        payload={**payload, "tenant_id": "demo"},
    )
    process_one()
    # second poll should find nothing — no follow-up slack_post was queued.
    assert claim_next() is None


def test_worker_enqueues_slack_post_when_configured(isolated, monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "slack_default_channel", "#oncall")
    payload = _alert_payload()
    enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key=payload["alert_id"],
        payload={**payload, "tenant_id": "demo"},
    )
    process_one()  # process the rca job
    nxt = claim_next()
    assert nxt is not None
    assert nxt.kind == "slack_post"
    assert nxt.tenant_id == "demo"


def test_fail_retries_with_backoff_then_gives_up(isolated) -> None:
    """fail(retry=True) flips status back to pending with a future
    next_attempt_at until attempts >= max_attempts, then marks failed."""
    from ai_oncall.jobs.store import _conn

    job = enqueue(
        kind="rca",
        tenant_id="demo",
        idempotency_key="x",
        payload={"alert_id": "x"},
        max_attempts=2,
    )
    # Simulate two claim+fail cycles. Each claim bumps attempts by 1.
    for cycle in range(2):
        # Force next_attempt_at into the past so claim_next picks it up
        # without waiting on real backoff.
        with _conn() as conn:
            conn.execute(
                "UPDATE jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
                (job.job_id,),
            )
        claimed = claim_next()
        assert claimed is not None, f"cycle {cycle}: queue should have job"
        result = fail(claimed.job_id, error=f"boom-{cycle}", retry=True)
        # After 2 attempts (== max_attempts), no more retries.
        if cycle == 0:
            assert result.status == "pending"
        else:
            assert result.status == "failed"


def test_idempotency_keys_isolated_across_tenants(isolated) -> None:
    """Same idempotency_key under different tenants is two distinct jobs."""
    j_a = enqueue(
        kind="rca",
        tenant_id="tenant-a",
        idempotency_key="shared-key",
        payload={"alert_id": "shared-key", "tenant_id": "tenant-a"},
    )
    j_b = enqueue(
        kind="rca",
        tenant_id="tenant-b",
        idempotency_key="shared-key",
        payload={"alert_id": "shared-key", "tenant_id": "tenant-b"},
    )
    assert j_a.job_id != j_b.job_id
