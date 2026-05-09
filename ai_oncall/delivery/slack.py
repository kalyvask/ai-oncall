"""Stage 6 — POST. Slack Block Kit message builder.

The function below produces a `blocks` payload that Slack's chat.postMessage
accepts. Top hypothesis is the parent message; alternatives render as a
threaded reply (composed by the caller). 👍/👎/"wrong root cause" reaction
hooks are wired in `delivery/reactions.py` and feed back into the LEARN store.

This module does NOT make HTTP calls — `send.py` (TODO once a customer
enables Slack) handles auth and transport. Keeping the formatter pure makes
golden-file tests trivial.
"""

from __future__ import annotations

from typing import Any

from ai_oncall.models import Hypothesis, RcaReport


def _confidence_emoji(conf: float) -> str:
    if conf >= 0.85:
        return "🔴"  # high confidence -> alarm-red
    if conf >= 0.6:
        return "🟠"
    return "🟡"


def _hypothesis_block(
    h: Hypothesis, *, top: bool, report_id: str | None = None
) -> list[dict[str, Any]]:
    header = (
        f"*Top hypothesis* {_confidence_emoji(h.confidence)}  "
        f"`{h.root_cause_service}` · {int(h.confidence * 100)}% confidence"
    ) if top else f"_Alt:_ `{h.root_cause_service}` · {int(h.confidence * 100)}%"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": h.reasoning}},
    ]
    evidence_lines = "\n".join(f"• {e.claim}  _({e.source})_" for e in h.evidence[:5])
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": evidence_lines}})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Recommended:* `{h.recommended_action}`"},
    })
    if h.runbook_link:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"runbook: `{h.runbook_link}`"}],
        })

    # Block Kit interactive buttons. Only render on the top hypothesis (the
    # parent message); alt hypotheses are read-only. The rollback button is
    # gated behind staged_action.tier == "propose" AND a kind that's worth
    # one-clicking. "noop" / "manual" describe situations where no button
    # would be safe.
    actionable_kinds = {"rollback", "restart", "scale", "feature_flag"}
    if (
        top
        and report_id
        and h.staged_action
        and h.staged_action.tier == "propose"
        and h.staged_action.kind in actionable_kinds
    ):
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": f"Approve {h.staged_action.kind}"},
                    "action_id": "approve_rollback" if h.staged_action.kind == "rollback" else f"approve_{h.staged_action.kind}",
                    "value": report_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Confirm action"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"This will run `{h.staged_action.kind}` on `{h.staged_action.service}`.",
                        },
                        "confirm": {"type": "plain_text", "text": "Run it"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Wrong root cause"},
                    "action_id": "mark_wrong_root_cause",
                    "value": report_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Pin as not flaky"},
                    "action_id": "pin_not_flaky",
                    "value": report_id,
                },
            ],
        })
    return blocks


def render_parent(report: RcaReport) -> list[dict[str, Any]]:
    """Slack blocks for the parent message — top hypothesis only.

    The caller posts this as the parent and posts `render_alternatives` as a
    reply in the same thread. Reactions on the parent feed back to LEARN.
    """
    top = report.hypotheses[0]
    # Embed `report_id` verbatim in a context block so thread Q&A's
    # `_resolve_report_id_from_event` can recover it from the parent message
    # text (Slack does not forward the parent's metadata to thread events).
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 {report.alert.title}"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"`{report.alert.service}` · {report.alert.severity}"},
                {"type": "mrkdwn", "text": f"tenant `{report.tenant_id}`"},
                {"type": "mrkdwn", "text": f"model `{report.model.id}`"},
                {"type": "mrkdwn", "text": f"id `{report.report_id}`"},
            ],
        },
        {"type": "divider"},
    ]
    blocks.extend(_hypothesis_block(top, top=True, report_id=report.report_id))
    if report.escalation and report.escalation.should_escalate:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *Escalation suggested:* {report.escalation.reason}"},
        })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "React 👍 if useful · 👎 if wrong · 🟥 'wrong root cause'"}],
    })
    return blocks


def render_alternatives(report: RcaReport) -> list[dict[str, Any]]:
    """Threaded reply with alternative hypotheses (rank 2..N)."""
    blocks: list[dict[str, Any]] = []
    for h in report.hypotheses[1:]:
        blocks.append({"type": "divider"})
        blocks.extend(_hypothesis_block(h, top=False))
    return blocks
