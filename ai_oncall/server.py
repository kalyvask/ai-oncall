"""FastAPI server.

Endpoints:
  GET  /health           → liveness, no tenant required.
  GET  /ready            → readiness (worker running, DB writable).
  POST /webhooks/alert   → Stage 1 RECEIVE. Validates and enqueues an RCA
                            job. Returns 202 with job_id + Location header.
  GET  /jobs/{job_id}    → job status + result.
  GET  /incidents        → list incidents for the request's tenant.
  GET  /incidents/{id}   → single incident with full RCA report.
  POST /feedback         → submit thumbs feedback against a report_id.
  GET  /topology         → tenant-scoped service graph.

The pipeline is durable: the alert webhook enqueues an ``rca`` job, the
async worker (started by lifespan) runs ``run_rca`` and enqueues a
``slack_post`` job for delivery with retries.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError as ModelValidationError

from ai_oncall.delivery.reactions import (
    handle_interaction,
    parse_interaction_payload,
    verify_slack_signature,
)
from ai_oncall.delivery.send import (
    SlackSendError,
    post_thread_reply,
)
from ai_oncall.delivery.thread_qa import answer_thread_question, render_answer_blocks
from ai_oncall.ingest.alerts import receive
from ai_oncall.jobs import enqueue as enqueue_job
from ai_oncall.jobs import get_job, list_jobs, run_worker
from ai_oncall.learnings.incidents import (
    get_incident,
    list_incidents,
    lookup_report_id_by_thread,
)
from ai_oncall.learnings.store import LearningRecord
from ai_oncall.learnings import store as learnings_store
from ai_oncall.llm.client import get_client as get_llm_client
from ai_oncall.logging_setup import configure
from ai_oncall.models import RcaReport
from ai_oncall.settings import settings
from ai_oncall.storage.factory import make_store
from ai_oncall.storage.tenancy import tenant_middleware
from ai_oncall.topology.builder import build as build_topology

configure()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background worker on startup; stop it on shutdown.

    Skipped when AI_ONCALL_DISABLE_WORKER=1 (tests drive the worker
    synchronously via ``process_one``)."""
    import os

    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None
    if os.environ.get("AI_ONCALL_DISABLE_WORKER") != "1":
        task = asyncio.create_task(run_worker(stop))
    app.state.worker_stop = stop
    app.state.worker_task = task
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()


app = FastAPI(title="ai-oncall", version="0.0.1", lifespan=_lifespan)
app.middleware("http")(tenant_middleware)


# Slack interactivity is open to the public internet (Slack POSTs from its
# own ranges) but each request is signature-verified. We exempt this path
# from the X-Tenant-Id middleware because Slack's own request can't carry
# our tenant header — the tenant is recovered from the persisted incident.
SLACK_PATH_PREFIX = "/webhooks/slack/"


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/ready")
def ready(request: Request) -> JSONResponse:
    """Readiness probe. True when the worker task is alive and the jobs DB
    is writable. Used by deployment platforms (k8s, Fly, Render) to decide
    when to route traffic."""
    worker_task = getattr(request.app.state, "worker_task", None)
    worker_ok = worker_task is None or not worker_task.done()
    try:
        from ai_oncall.jobs.store import _conn

        with _conn() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    ok = worker_ok and db_ok
    return JSONResponse(
        {"ok": ok, "worker": worker_ok, "jobs_db": db_ok},
        status_code=200 if ok else 503,
    )


def _verify_webhook_signature(raw_body: bytes, header_value: str | None) -> None:
    """Verify ``X-Signature: hmac-sha256=<hex>`` against the raw body.

    Skipped silently when no signing secret is configured (dev mode). Raises
    ``HTTPException(401)`` when the secret is configured but verification
    fails. Constant-time compare to avoid timing leaks."""
    import hashlib
    import hmac as _hmac

    secret = settings.webhook_signing_secret
    if not secret:
        return  # dev mode
    if not header_value:
        raise HTTPException(401, "X-Signature header required when webhook_signing_secret is set")
    scheme, _, hex_digest = header_value.partition("=")
    if scheme.strip().lower() != "hmac-sha256" or not hex_digest:
        raise HTTPException(401, "X-Signature must be 'hmac-sha256=<hex>'")
    expected = _hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(hex_digest.strip(), expected):
        raise HTTPException(401, "X-Signature did not match")


@app.post("/webhooks/alert", status_code=202)
async def alert_webhook(request: Request) -> JSONResponse:
    """Validate the alert, enqueue an RCA job, return 202.

    Idempotent on ``(tenant_id, alert_id)``: a duplicate POST returns the
    existing job rather than creating a second one. Callers should poll
    ``GET /jobs/{job_id}`` for status.

    When ``AI_ONCALL_WEBHOOK_SIGNING_SECRET`` is configured, every request
    must include ``X-Signature: hmac-sha256=<hex>`` computed over the raw
    body. Unsigned posts return 401.
    """
    tenant_id: str = request.state.tenant_id
    raw_body = await request.body()
    _verify_webhook_signature(raw_body, request.headers.get("X-Signature"))
    payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    if "tenant_id" in payload and payload["tenant_id"] != tenant_id:
        raise HTTPException(400, "payload tenant_id does not match X-Tenant-Id header")
    payload.setdefault("tenant_id", tenant_id)
    try:
        alert = receive(payload)
    except (SchemaValidationError, ModelValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc

    job = enqueue_job(
        kind="rca",
        tenant_id=tenant_id,
        idempotency_key=alert.alert_id,
        payload=alert.model_dump(mode="json"),
    )
    return JSONResponse(
        {
            "job_id": job.job_id,
            "status": job.status,
            "alert_id": alert.alert_id,
            "status_url": f"/jobs/{job.job_id}",
        },
        status_code=202,
        headers={"Location": f"/jobs/{job.job_id}"},
    )


@app.get("/metrics")
def metrics_endpoint() -> Response:
    """Prometheus-format metrics. Tenant-agnostic counts; tenant-scoped
    metrics live behind the auth boundary added in P1-9.

    Counters emitted: pending/running/done/failed job totals plus an alert
    on the most concerning state (failed jobs in last 15 min)."""
    from datetime import datetime, timedelta, timezone

    from ai_oncall.jobs.store import _conn

    now = datetime.now(timezone.utc)
    window = (now - timedelta(minutes=15)).isoformat()
    with _conn() as conn:
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        }
        recent_failures = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='failed' AND updated_at >= ?",
            (window,),
        ).fetchone()["n"]
    lines = [
        "# HELP ai_oncall_jobs_total Total jobs by status.",
        "# TYPE ai_oncall_jobs_total gauge",
        f'ai_oncall_jobs_total{{status="pending"}} {counts.get("pending", 0)}',
        f'ai_oncall_jobs_total{{status="running"}} {counts.get("running", 0)}',
        f'ai_oncall_jobs_total{{status="done"}} {counts.get("done", 0)}',
        f'ai_oncall_jobs_total{{status="failed"}} {counts.get("failed", 0)}',
        "# HELP ai_oncall_jobs_failed_last_15m Jobs that landed in `failed` in the last 15 minutes.",
        "# TYPE ai_oncall_jobs_failed_last_15m gauge",
        f"ai_oncall_jobs_failed_last_15m {recent_failures}",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/jobs")
def jobs_list(request: Request, status: str | None = None, limit: int = 50) -> JSONResponse:
    """Tenant-scoped job list. ``?status=failed`` is the dead-letter view."""
    tenant_id: str = request.state.tenant_id
    valid: tuple[str, ...] = ("pending", "running", "done", "failed")
    if status is not None and status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    rows = list_jobs(tenant_id=tenant_id, status=status, limit=limit)  # type: ignore[arg-type]
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "items": [
                {
                    "job_id": r.job_id,
                    "kind": r.kind,
                    "status": r.status,
                    "attempts": r.attempts,
                    "max_attempts": r.max_attempts,
                    "error": r.error,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in rows
            ],
        }
    )


@app.get("/jobs/{job_id}")
def job_status(job_id: str, request: Request) -> JSONResponse:
    """Return the current job status. Restricted to the request's tenant."""
    tenant_id: str = request.state.tenant_id
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no job for id={job_id}")
    if job.tenant_id != tenant_id:
        raise HTTPException(404, f"no job for id={job_id}")
    return JSONResponse(
        {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "result": job.result(),
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
    )


@app.get("/incidents")
def list_incidents_endpoint(request: Request, limit: int = 25) -> JSONResponse:
    tenant_id: str = request.state.tenant_id
    rows = list_incidents(tenant_id=tenant_id, limit=limit)
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "items": [
                {
                    "report_id": r.report_id,
                    "alert_id": r.alert_id,
                    "service": r.service,
                    "root_cause_service": r.root_cause_service,
                    "root_cause_class": r.root_cause_class,
                    "top_confidence": r.top_confidence,
                    "abstained": r.abstained,
                    "trust_tier": r.trust_tier,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }
    )


@app.get("/incidents/{report_id}/diff/{other_id}")
def incident_diff(report_id: str, other_id: str, request: Request) -> JSONResponse:
    """Diff two incidents — typically the same alert run at two model or
    prompt versions. Returns a structured comparison of the top hypothesis,
    investigation cost/latency, and tool-call signatures. Both incidents
    must belong to the request's tenant."""
    tenant_id: str = request.state.tenant_id
    a = get_incident(report_id)
    b = get_incident(other_id)
    if a is None or a.tenant_id != tenant_id:
        raise HTTPException(404, f"no incident for id={report_id}")
    if b is None or b.tenant_id != tenant_id:
        raise HTTPException(404, f"no incident for id={other_id}")
    ra, rb = a.report(), b.report()

    def _summary(r: RcaReport) -> dict[str, Any]:
        top = r.hypotheses[0] if r.hypotheses else None
        inv = r.investigation
        return {
            "report_id": r.report_id,
            "model_id": r.model.id if r.model else None,
            "alert_id": r.alert.alert_id,
            "top_root_cause_service": top.root_cause_service if top else None,
            "top_confidence": top.confidence if top else None,
            "evidence_count": len(top.evidence) if top else 0,
            "hypothesis_services": [h.root_cause_service for h in r.hypotheses],
            "tool_call_signature": [tc.tool for tc in (inv.tool_calls if inv else [])],
            "tokens_in": getattr(inv, "tokens_in", None) if inv else None,
            "tokens_out": getattr(inv, "tokens_out", None) if inv else None,
            "cost_usd": getattr(inv, "cost_usd", None) if inv else None,
            "abstained": (r.calibration.abstain if r.calibration else None),
            "abstention_codes": ([x.code for x in r.calibration.reasons] if r.calibration else []),
        }

    sa, sb = _summary(ra), _summary(rb)
    return JSONResponse(
        {
            "a": sa,
            "b": sb,
            "agreement": {
                "same_root_cause": sa["top_root_cause_service"] == sb["top_root_cause_service"],
                "confidence_delta": ((sa["top_confidence"] or 0) - (sb["top_confidence"] or 0)),
                "same_tool_calls": sa["tool_call_signature"] == sb["tool_call_signature"],
                "same_abstention_codes": sa["abstention_codes"] == sb["abstention_codes"],
            },
        }
    )


@app.get("/sloreport")
def slo_report(request: Request, limit: int = 50) -> JSONResponse:
    """Per-tenant SLO snapshot: p50/p95 latency and cost across the most
    recent ``limit`` incidents. Used by the UI's ops dashboard and by the
    deployment pipeline to gate releases on SLO regressions."""
    tenant_id: str = request.state.tenant_id
    rows = list_incidents(tenant_id=tenant_id, limit=limit)
    latencies_ms: list[float] = []
    costs_usd: list[float] = []
    slo_violations = 0
    cost_violations = 0
    for row in rows:
        try:
            report = row.report()
        except Exception:
            continue
        inv = report.investigation
        if inv is None:
            continue
        ms = (
            getattr(inv, "total_latency_ms", None)
            if not isinstance(inv, dict)
            else inv.get("total_latency_ms")
        )
        if ms is None and hasattr(inv, "model_extra") and inv.model_extra:
            ms = inv.model_extra.get("total_latency_ms")
        if ms is not None:
            latencies_ms.append(float(ms))
            if hasattr(inv, "model_extra") and (inv.model_extra or {}).get("slo_violated"):
                slo_violations += 1
        if inv.cost_usd is not None:
            costs_usd.append(float(inv.cost_usd))
            if hasattr(inv, "model_extra") and (inv.model_extra or {}).get("cost_violated"):
                cost_violations += 1

    def _pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        idx = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
        return s[idx]

    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "incidents_sampled": len(rows),
            "latency_budget_seconds": settings.latency_budget_seconds,
            "cost_ceiling_usd": settings.cost_ceiling_usd,
            "latency_ms": {
                "p50": _pct(latencies_ms, 50),
                "p95": _pct(latencies_ms, 95),
                "p99": _pct(latencies_ms, 99),
                "samples": len(latencies_ms),
            },
            "cost_usd": {
                "p50": _pct(costs_usd, 50),
                "p95": _pct(costs_usd, 95),
                "samples": len(costs_usd),
            },
            "slo_violations": slo_violations,
            "cost_violations": cost_violations,
        }
    )


@app.get("/incidents/{report_id}")
def incident_detail(report_id: str, request: Request) -> JSONResponse:
    tenant_id: str = request.state.tenant_id
    incident = get_incident(report_id)
    if incident is None or incident.tenant_id != tenant_id:
        raise HTTPException(404, f"no incident for id={report_id}")
    return JSONResponse(incident.report().model_dump(mode="json"))


@app.get("/incidents/{report_id}/trace")
def incident_trace(report_id: str, request: Request) -> JSONResponse:
    """Return just the tool-call trace + reasoning for the UI's trace panel."""
    tenant_id: str = request.state.tenant_id
    incident = get_incident(report_id)
    if incident is None or incident.tenant_id != tenant_id:
        raise HTTPException(404, f"no incident for id={report_id}")
    report = incident.report()
    return JSONResponse(
        {
            "report_id": report.report_id,
            "alert": report.alert.model_dump(mode="json"),
            "tool_calls": [
                tc.model_dump(mode="json")
                for tc in (
                    (report.investigation.tool_calls or [])
                    if report.investigation is not None
                    else []
                )
            ],
            "hypotheses": [h.model_dump(mode="json") for h in report.hypotheses],
        }
    )


@app.post("/actions/{report_id}/approve")
async def approve_action(report_id: str, request: Request) -> JSONResponse:
    """Approve the staged action for an incident.

    Body: ``{user_id?, user_name?, dry_run?}``. When ``dry_run=true``, returns
    the action preview (kind, service, tier, blast radius, command) without
    dispatching. Use this to render a confirmation modal before the real call.
    Routed through the same dispatch path as the Slack one-click button so
    auditing and refusal rules stay consistent.
    """
    from ai_oncall.agent.policy import DEFAULT_POLICY
    from ai_oncall.delivery.cd_dispatch import dispatch_rollback
    from ai_oncall.delivery.reactions import _audit_action

    tenant_id: str = request.state.tenant_id
    incident = get_incident(report_id)
    if incident is None or incident.tenant_id != tenant_id:
        raise HTTPException(404, f"no incident for id={report_id}")
    report = incident.report()
    top = report.hypotheses[0] if report.hypotheses else None
    if top is None or top.staged_action is None:
        raise HTTPException(409, "no staged action on top hypothesis")
    if top.staged_action.tier != "propose":
        raise HTTPException(
            403,
            f"staged action tier is '{top.staged_action.tier}'; approve only fires for 'propose'",
        )

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_id = body.get("user_id", "api-user")
    user_name = body.get("user_name", "api-user")
    dry_run = bool(body.get("dry_run", False))

    blast = DEFAULT_POLICY.blast_radius_for(top.staged_action.kind)
    preview = {
        "kind": top.staged_action.kind,
        "service": top.staged_action.service,
        "tier": top.staged_action.tier,
        "blast_radius": blast,
        "rationale": top.staged_action.rationale,
        "runbook_ref": top.staged_action.runbook_ref,
        "top_confidence": top.confidence,
    }

    if dry_run:
        return JSONResponse({"ok": True, "dry_run": True, "preview": preview})

    success, detail = dispatch_rollback(
        action=top.staged_action,
        report_id=report_id,
        user_id=user_id,
        user_name=user_name,
        target_url=settings.cd_dispatch_url,
    )
    _audit_action(
        report_id=report_id,
        action_id="approve_rollback",
        user_id=user_id,
        user_name=user_name,
        success=success,
        detail=f"blast={blast}; {detail}",
    )
    return JSONResponse({"ok": success, "detail": detail, "preview": preview})


@app.post("/feedback")
async def feedback_endpoint(request: Request) -> JSONResponse:
    """Record thumbs feedback against a report_id. Body: {report_id, reaction,
    correction?}. reaction is one of: thumbs_up, thumbs_down, wrong_root_cause."""
    tenant_id: str = request.state.tenant_id
    payload = await request.json()
    report_id = payload.get("report_id")
    reaction = payload.get("reaction")
    if not report_id or not reaction:
        raise HTTPException(400, "report_id and reaction are required")
    incident = get_incident(report_id)
    if incident is None or incident.tenant_id != tenant_id:
        raise HTTPException(404, f"no incident for id={report_id}")
    report = incident.report()
    record = LearningRecord(
        tenant_id=tenant_id,
        report_id=report_id,
        alert_title=report.alert.title,
        service=report.alert.service,
        top_hypothesis=report.hypotheses[0].root_cause_service if report.hypotheses else "",
        confidence=report.hypotheses[0].confidence if report.hypotheses else 0.0,
        reaction=reaction,
        correction=payload.get("correction") or "via API",
    )
    learnings_store.LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with learnings_store.LEARNINGS_PATH.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")
    return JSONResponse({"ok": True, "report_id": report_id, "reaction": reaction})


@app.get("/topology")
def topology(request: Request) -> JSONResponse:
    store = make_store()
    snapshot = build_topology(request.state.tenant_id, store)
    return JSONResponse(snapshot.model_dump(mode="json", by_alias=True))


@app.post("/webhooks/slack/action")
async def slack_action(request: Request) -> JSONResponse:
    """Receive a Slack Block Kit interaction (button click).

    Slack posts ``application/x-www-form-urlencoded`` with a single
    ``payload`` field carrying JSON. We verify the X-Slack-Signature first
    against the raw body before parsing.

    On success, returns a Block Kit ack the Slack client can render.
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(
        signing_secret=settings.slack_signing_secret or "",
        request_body=body,
        timestamp=timestamp,
        signature=signature,
    ):
        raise HTTPException(401, "invalid Slack signature")

    try:
        payload = parse_interaction_payload(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    outcomes = handle_interaction(payload)
    # Slack expects a 200 even for application errors; encode them in the body.
    return JSONResponse(
        {
            "outcomes": [
                {
                    "action_id": o.action_id,
                    "report_id": o.report_id,
                    "user_id": o.user_id,
                    "user_name": o.user_name,
                    "success": o.success,
                    "detail": o.detail,
                }
                for o in outcomes
            ]
        }
    )


@app.post("/webhooks/slack/event")
async def slack_event(request: Request) -> JSONResponse:
    """Slack Events API endpoint.

    Handles two cases:
    1. ``url_verification`` — Slack's first-time-setup handshake. Echo the
       ``challenge`` field back unmodified.
    2. ``event_callback`` with a ``message`` event in a thread — extract the
       ``thread_ts``, look up the report by ``thread_ts → report_id`` (the
       reverse map is populated when we POST the parent message; for now we
       require the parent message to embed report_id in metadata), run a
       scoped follow-up investigation via thread_qa, and return Block Kit.

    The signature check is identical to ``/slack/action``.
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(
        signing_secret=settings.slack_signing_secret or "",
        request_body=body,
        timestamp=timestamp,
        signature=signature,
    ):
        raise HTTPException(401, "invalid Slack signature")

    payload = await request.json()

    # 1. URL verification handshake.
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    # 2. Thread reply handler.
    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True, "ignored": "not an event_callback"})

    event = payload.get("event") or {}
    if event.get("type") != "message" or event.get("subtype") in {"bot_message", "message_changed"}:
        return JSONResponse({"ok": True, "ignored": "not a user message"})
    if not event.get("thread_ts") or event.get("thread_ts") == event.get("ts"):
        # Top-level message (or self-thread). Thread Q&A only fires on replies.
        return JSONResponse({"ok": True, "ignored": "not a thread reply"})

    question = event.get("text", "").strip()
    if not question:
        return JSONResponse({"ok": True, "ignored": "empty text"})

    # Resolution priority:
    # 1. Persisted (channel, thread_ts) -> report_id mapping (delivery/send
    #    populates this on every parent post; most reliable).
    # 2. report_id embedded in the parent message context block (regex).
    # 3. Explicit `report_id` field on the event payload (test/manual hook).
    channel = event.get("channel") or ""
    thread_ts = event.get("thread_ts") or ""
    report_id: str | None = None
    if channel and thread_ts:
        report_id = lookup_report_id_by_thread(channel=channel, thread_ts=thread_ts)
    if not report_id:
        report_id = _resolve_report_id_from_event(event) or event.get("report_id")
    if not report_id:
        return JSONResponse({"ok": False, "ignored": "could not resolve report_id from thread"})

    incident = get_incident(report_id)
    if incident is None:
        return JSONResponse({"ok": False, "ignored": f"no report found for {report_id}"})
    report = incident.report()

    store = make_store()
    answer = answer_thread_question(
        report=report, question=question, store=store, llm=get_llm_client()
    )
    blocks = render_answer_blocks(answer)

    # Best-effort post back to the thread. Falls back to returning the blocks
    # in the JSON response so a non-Slack client can still drive this endpoint.
    posted_ts: str | None = None
    if channel and thread_ts and settings.slack_bot_token:
        try:
            posted_ts = post_thread_reply(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=answer.summary[:140],
            )
        except SlackSendError as e:
            # Don't 500 the request; surface the error in the response so the
            # operator sees it but the user's question is not lost.
            return JSONResponse(
                {
                    "ok": False,
                    "blocks": blocks,
                    "thread_ts": thread_ts,
                    "send_error": str(e),
                }
            )

    return JSONResponse(
        {
            "ok": True,
            "blocks": blocks,
            "thread_ts": thread_ts,
            "posted_ts": posted_ts,
        }
    )


_REPORT_ID_RE = re.compile(
    # UUID-style (what models.py emits today) OR rpt_<id> / report_<id>.
    r"\b("
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|rpt_[A-Za-z0-9_-]+"
    r"|report_[A-Za-z0-9_-]+"
    r")\b",
    re.IGNORECASE,
)


def _resolve_report_id_from_event(event: dict[str, Any]) -> str | None:
    """Recover the report_id from the parent message text.

    `delivery/slack.py` embeds `id ``<report_id>``` in the parent's context
    block (a code-fenced token). Slack passes that in the thread event's
    ``message`` payload — the regex grabs it back out.

    Falls back to message attachments + the user's reply text on the off
    chance the user paste-quoted the parent.
    """
    candidates: list[str] = []
    msg = event.get("message") or {}
    if isinstance(msg, dict):
        candidates.append(str(msg.get("text", "")))
        for block in msg.get("blocks") or []:
            for elt in block.get("elements") or []:
                if isinstance(elt, dict) and elt.get("text"):
                    candidates.append(str(elt["text"]))
    if event.get("attachments"):
        for att in event["attachments"]:
            if isinstance(att, dict) and att.get("text"):
                candidates.append(str(att["text"]))
    candidates.append(str(event.get("text", "")))

    for chunk in candidates:
        match = _REPORT_ID_RE.search(chunk)
        if match:
            return match.group(1)
    return None
