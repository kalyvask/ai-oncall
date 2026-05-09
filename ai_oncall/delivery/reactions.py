"""Slack interactivity handlers.

Two surfaces, one trust posture:

1. **Reactions** (👍 / 👎 / 🟥). Plain emoji reactions on the parent message
   are the lightest-weight feedback. Translated to a `Reaction` literal and
   appended to ``learnings.jsonl`` and the incident persistence layer.

2. **Block Kit actions** (`Approve rollback`, `Mark wrong root cause`,
   `Pin as not flaky`). These are signed Slack interaction payloads. Each
   carries a verified user identity and a typed action_id; the handler
   verifies the Slack signature, dispatches by action_id, and writes an
   audit row.

The signature verifier is a stand-alone function so unit tests can hit it
without spinning up FastAPI. The transport (HTTP POST to Slack) lives in
``delivery/send.py`` (TODO) — this module only handles inbound payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib.parse import parse_qs

from ai_oncall.delivery.cd_dispatch import dispatch_rollback
from ai_oncall.learnings.incidents import get_incident
from ai_oncall.learnings import store as learnings_store
from ai_oncall.learnings.store import LearningRecord
from ai_oncall.settings import settings

logger = logging.getLogger(__name__)


# --- types ---------------------------------------------------------------


ActionId = Literal[
    "approve_rollback",
    "mark_wrong_root_cause",
    "pin_not_flaky",
]


@dataclass(frozen=True)
class ActionOutcome:
    """The outcome of handling a Slack action — for the audit log."""

    action_id: str
    report_id: str
    user_id: str
    user_name: str
    success: bool
    detail: str = ""
    handled_at: datetime = datetime.now(timezone.utc)


# --- signature verification ----------------------------------------------


def verify_slack_signature(
    *,
    signing_secret: str,
    request_body: bytes,
    timestamp: str,
    signature: str,
    max_skew_seconds: int = 60 * 5,
) -> bool:
    """Verify a Slack request signature.

    Spec: https://api.slack.com/authentication/verifying-requests-from-slack

    Caller passes the raw request body (bytes) plus the X-Slack-Request-Timestamp
    and X-Slack-Signature headers. We refuse anything older than 5 minutes by
    default (replay protection).
    """
    if not signing_secret:
        # Refuse to verify when a secret isn't configured; saying "yes" here
        # would be silently insecure.
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > max_skew_seconds:
        return False
    base = f"v0:{timestamp}:".encode("utf-8") + request_body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    # Constant-time compare.
    return hmac.compare_digest(expected, signature or "")


# --- payload parsing -----------------------------------------------------


def parse_interaction_payload(body: bytes) -> dict[str, Any]:
    """Slack posts interaction payloads as application/x-www-form-urlencoded
    with a single ``payload=<json>`` field. Decode that here.
    """
    parsed = parse_qs(body.decode("utf-8"))
    raw_payload = parsed.get("payload", [None])[0]
    if not raw_payload:
        raise ValueError("Slack interaction payload missing 'payload' field")
    return json.loads(raw_payload)


# --- action handlers -----------------------------------------------------


def handle_interaction(
    payload: dict[str, Any],
    *,
    cd_dispatch_url: Optional[str] = None,
) -> list[ActionOutcome]:
    """Route a parsed Slack interaction payload to its handler.

    A single payload can carry multiple actions (Slack's spec); we handle
    each independently and return one outcome per action. Failures in one
    handler do not abort the others.
    """
    actions = payload.get("actions") or []
    user = payload.get("user") or {}
    user_id = user.get("id", "unknown")
    user_name = user.get("name") or user.get("username", "unknown")

    outcomes: list[ActionOutcome] = []
    for action in actions:
        action_id = action.get("action_id", "")
        # action.value is set when we render the button; we encode the report_id there.
        report_id = action.get("value") or _extract_report_id(payload)

        if not report_id:
            outcomes.append(
                ActionOutcome(
                    action_id=action_id,
                    report_id="",
                    user_id=user_id,
                    user_name=user_name,
                    success=False,
                    detail="missing report_id in action.value",
                )
            )
            continue

        outcome = _dispatch(
            action_id=action_id,
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            cd_dispatch_url=cd_dispatch_url,
        )
        outcomes.append(outcome)

    return outcomes


def _dispatch(
    *,
    action_id: str,
    report_id: str,
    user_id: str,
    user_name: str,
    cd_dispatch_url: Optional[str],
) -> ActionOutcome:
    if action_id == "approve_rollback":
        return _handle_approve_rollback(
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            cd_dispatch_url=cd_dispatch_url,
        )
    if action_id == "mark_wrong_root_cause":
        return _handle_feedback_reaction(
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            reaction="wrong_root_cause",
        )
    if action_id == "pin_not_flaky":
        return _handle_feedback_reaction(
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            reaction="thumbs_up",
        )
    return ActionOutcome(
        action_id=action_id,
        report_id=report_id,
        user_id=user_id,
        user_name=user_name,
        success=False,
        detail=f"unknown action_id: {action_id}",
    )


def _handle_approve_rollback(
    *,
    report_id: str,
    user_id: str,
    user_name: str,
    cd_dispatch_url: Optional[str],
) -> ActionOutcome:
    incident = get_incident(report_id)
    if incident is None:
        return ActionOutcome(
            action_id="approve_rollback",
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            success=False,
            detail="report not found",
        )
    report = incident.report()
    top = report.hypotheses[0] if report.hypotheses else None
    if top is None or top.staged_action is None:
        return ActionOutcome(
            action_id="approve_rollback",
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            success=False,
            detail="no staged action on top hypothesis",
        )
    if top.staged_action.tier != "propose":
        # `auto` already ran; `recommend` was never proposed for one-click.
        return ActionOutcome(
            action_id="approve_rollback",
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            success=False,
            detail=(
                f"staged action tier is '{top.staged_action.tier}'; "
                "approve_rollback only fires for 'propose'"
            ),
        )

    target_url = cd_dispatch_url or settings.cd_dispatch_url  # type: ignore[attr-defined]
    success, detail = dispatch_rollback(
        action=top.staged_action,
        report_id=report_id,
        user_id=user_id,
        user_name=user_name,
        target_url=target_url,
    )
    _audit_action(
        report_id=report_id,
        action_id="approve_rollback",
        user_id=user_id,
        user_name=user_name,
        success=success,
        detail=detail,
    )
    return ActionOutcome(
        action_id="approve_rollback",
        report_id=report_id,
        user_id=user_id,
        user_name=user_name,
        success=success,
        detail=detail,
    )


def _handle_feedback_reaction(
    *,
    report_id: str,
    user_id: str,
    user_name: str,
    reaction: str,
) -> ActionOutcome:
    incident = get_incident(report_id)
    if incident is None:
        return ActionOutcome(
            action_id=f"feedback:{reaction}",
            report_id=report_id,
            user_id=user_id,
            user_name=user_name,
            success=False,
            detail="report not found",
        )
    report = incident.report()
    record = LearningRecord(
        tenant_id=report.tenant_id,
        report_id=report_id,
        alert_title=report.alert.title,
        service=report.alert.service,
        top_hypothesis=report.hypotheses[0].root_cause_service,
        confidence=report.hypotheses[0].confidence,
        reaction=reaction,  # type: ignore[arg-type]
        correction=f"via Slack action by {user_name} ({user_id})",
    )
    learnings_store.LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with learnings_store.LEARNINGS_PATH.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")
    return ActionOutcome(
        action_id=f"feedback:{reaction}",
        report_id=report_id,
        user_id=user_id,
        user_name=user_name,
        success=True,
        detail=f"reaction={reaction}",
    )


def _audit_action(
    *,
    report_id: str,
    action_id: str,
    user_id: str,
    user_name: str,
    success: bool,
    detail: str,
) -> None:
    """Append an audit row to learnings.jsonl. Best-effort."""
    try:
        LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "kind": "slack_action",
            "report_id": report_id,
            "action_id": action_id,
            "user_id": user_id,
            "user_name": user_name,
            "success": success,
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        with LEARNINGS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # pragma: no cover
        logger.exception("audit_action_failed")


def _extract_report_id(payload: dict[str, Any]) -> Optional[str]:
    """If the action's value isn't set, look in metadata, blocks, or callback id."""
    md = payload.get("message", {}).get("metadata") or {}
    if isinstance(md, dict):
        rid = md.get("event_payload", {}).get("report_id")
        if rid:
            return rid
    return payload.get("callback_id")
