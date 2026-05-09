"""FastAPI server.

Endpoints today:
  GET  /health           → liveness, no tenant required.
  POST /webhooks/alert   → Stage 1 RECEIVE. Validates payload, returns the
                            normalized Alert. Tenant comes from X-Tenant-Id;
                            mismatched payload tenant_id is a 400.
  GET  /topology         → Stage 2 ASSEMBLE. Static-yaml snapshot scoped to
                            the request's tenant.

The full agent loop (PLAN+INVESTIGATE+SYNTHESIZE+POST) is added in BRIEF.md
step 6 onwards.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
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
from ai_oncall.learnings.incidents import get_incident, lookup_report_id_by_thread
from ai_oncall.llm.client import get_client as get_llm_client
from ai_oncall.logging_setup import configure
from ai_oncall.settings import settings
from ai_oncall.storage.factory import make_store
from ai_oncall.storage.tenancy import tenant_middleware
from ai_oncall.topology.builder import build as build_topology

configure()
app = FastAPI(title="ai-oncall", version="0.0.1")
app.middleware("http")(tenant_middleware)


# Slack interactivity is open to the public internet (Slack POSTs from its
# own ranges) but each request is signature-verified. We exempt this path
# from the X-Tenant-Id middleware because Slack's own request can't carry
# our tenant header — the tenant is recovered from the persisted incident.
SLACK_PATH_PREFIX = "/webhooks/slack/"


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/webhooks/alert")
async def alert_webhook(request: Request) -> JSONResponse:
    tenant_id: str = request.state.tenant_id
    payload: dict[str, Any] = await request.json()
    if "tenant_id" in payload and payload["tenant_id"] != tenant_id:
        raise HTTPException(400, "payload tenant_id does not match X-Tenant-Id header")
    payload.setdefault("tenant_id", tenant_id)
    try:
        alert = receive(payload)
    except (SchemaValidationError, ModelValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(alert.model_dump(mode="json"))


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
        return JSONResponse(
            {"ok": False, "ignored": "could not resolve report_id from thread"}
        )

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
            for elt in (block.get("elements") or []):
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
