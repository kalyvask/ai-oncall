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


def _hypothesis_block(h: Hypothesis, *, top: bool) -> list[dict[str, Any]]:
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
    return blocks


def render_parent(report: RcaReport) -> list[dict[str, Any]]:
    """Slack blocks for the parent message — top hypothesis only.

    The caller posts this as the parent and posts `render_alternatives` as a
    reply in the same thread. Reactions on the parent feed back to LEARN.
    """
    top = report.hypotheses[0]
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
            ],
        },
        {"type": "divider"},
    ]
    blocks.extend(_hypothesis_block(top, top=True))
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
