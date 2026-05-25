"""Async worker loop. Polls the job table, dispatches by kind, retries on
failure. Designed to run as an asyncio task started by FastAPI's lifespan.

Single-process v1: one worker task per FastAPI process, exclusive on the
SQLite job DB. For multi-replica deployments, swap the claim_next implementation
for a real queue (Postgres SKIP LOCKED, Redis, SQS) without touching the
dispatch logic here.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ai_oncall.agent.run import run_rca
from ai_oncall.ingest.alerts import receive
from ai_oncall.jobs.store import JobRecord, claim_next, complete, enqueue, fail
from ai_oncall.learnings.incidents import get_incident
from ai_oncall.llm.client import get_client as get_llm_client
from ai_oncall.settings import settings
from ai_oncall.storage.factory import make_store

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5


def process_one() -> JobRecord | None:
    """Claim and process a single job synchronously. Returns the updated
    record, or None if the queue was empty. Public so tests can drive the
    worker without spinning up the asyncio loop."""
    job = claim_next()
    if job is None:
        return None
    try:
        if job.kind == "rca":
            return _process_rca(job)
        if job.kind == "slack_post":
            return _process_slack_post(job)
        return fail(job.job_id, error=f"unknown job kind: {job.kind}", retry=False)
    except Exception as exc:
        logger.exception("job_failed", extra={"job_id": job.job_id, "kind": job.kind})
        return fail(job.job_id, error=f"{type(exc).__name__}: {exc}")


def _process_rca(job: JobRecord) -> JobRecord:
    import time

    from ai_oncall.learnings.incidents import _conn as _incidents_conn

    payload = job.payload()
    alert = receive(payload)
    store = make_store()
    llm = get_llm_client()

    started = time.monotonic()
    report = run_rca(alert, store, llm, skip_slack=True)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    slo_violated = elapsed_ms > (settings.latency_budget_seconds * 1000.0)
    cost_usd = getattr(llm, "cumulative_cost_usd", 0.0)
    cost_violated = cost_usd > settings.cost_ceiling_usd if cost_usd > 0 else False

    # Annotate the persisted report row with SLO data so /sloreport can
    # aggregate p50/p95 without re-running. report_json is the source of
    # truth so update it via INSERT OR REPLACE through save_incident's
    # interface — for v1 we patch the JSON in place to avoid a second write
    # path.
    try:
        with _incidents_conn() as conn:
            row = conn.execute(
                "SELECT report_json FROM incidents WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if row is not None:
                blob = json.loads(row["report_json"])
                inv = blob.setdefault("investigation", {}) or {}
                inv["total_latency_ms"] = elapsed_ms
                inv["slo_violated"] = slo_violated
                if cost_usd > 0:
                    inv["cost_usd"] = cost_usd
                    inv["cost_violated"] = cost_violated
                blob["investigation"] = inv
                conn.execute(
                    "UPDATE incidents SET report_json=? WHERE report_id=?",
                    (json.dumps(blob), report.report_id),
                )
    except Exception:
        logger.exception("slo_annotate_failed", extra={"report_id": report.report_id})

    if settings.slack_bot_token and settings.slack_default_channel:
        enqueue(
            kind="slack_post",
            tenant_id=job.tenant_id,
            idempotency_key=report.report_id,
            payload={"report_id": report.report_id, "channel": settings.slack_default_channel},
        )

    complete(
        job.job_id,
        result={
            "report_id": report.report_id,
            "top_confidence": report.hypotheses[0].confidence if report.hypotheses else 0.0,
            "total_latency_ms": round(elapsed_ms, 1),
            "slo_violated": slo_violated,
            "cost_usd": round(cost_usd, 6) if cost_usd else 0.0,
        },
    )
    return job


def _process_slack_post(job: JobRecord) -> JobRecord:
    from ai_oncall.delivery.send import SlackSendError, post_rca

    payload = job.payload()
    report_id = payload["report_id"]
    channel = payload["channel"]
    incident = get_incident(report_id)
    if incident is None:
        return fail(job.job_id, error=f"no incident for report_id={report_id}", retry=False)
    try:
        ts = post_rca(incident.report(), channel)
    except SlackSendError as e:
        return fail(
            job.job_id,
            error=str(e),
            delay_seconds=getattr(e, "retry_after_seconds", None),
        )
    complete(job.job_id, result={"channel": channel, "thread_ts": ts})
    return job


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    """Main worker loop. Polls for jobs and processes them until stop_event
    is set. Designed for FastAPI lifespan: create one task at startup, set
    the event at shutdown."""
    stop_event = stop_event or asyncio.Event()
    logger.info("worker_started")
    while not stop_event.is_set():
        # Run the synchronous SQLite work off the event loop so we don't
        # block other handlers. The DB is tiny so this is fine.
        job = await asyncio.to_thread(process_one)
        if job is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    logger.info("worker_stopped")
