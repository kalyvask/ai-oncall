"""Slack ``chat.postMessage`` transport.

The renderers in ``delivery/slack.py`` produce Block Kit payloads. This
module is the seam that posts those blocks to a real Slack workspace and
threads the reply messages correctly.

Two surfaces:

- ``post_rca(report, channel, sender)`` — posts the parent message
  (``render_parent``) plus the alternatives reply (``render_alternatives``)
  in the same thread. Persists the ``(thread_ts, channel) -> report_id``
  mapping to ``data/incidents.sqlite`` so future Events API messages on
  this thread can recover the report.

- ``post_thread_reply(channel, thread_ts, blocks, sender)`` — posts an
  arbitrary block list as a threaded reply. Used by the Slack thread Q&A
  endpoint after computing an answer.

Errors are surfaced via ``SlackSendError``. The caller decides whether to
retry; this module does no retries on its own (Slack rate-limit responses
include a ``Retry-After`` header which a future scheduler should honor).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from ai_oncall.delivery.slack import render_alternatives, render_parent
from ai_oncall.learnings.incidents import (
    record_thread_mapping,
)
from ai_oncall.models import RcaReport
from ai_oncall.settings import settings

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"
DEFAULT_TIMEOUT_SECONDS = 5.0


class SlackSendError(RuntimeError):
    """Raised when chat.postMessage fails or returns ``ok: false``.

    ``retry_after_seconds`` is set when Slack returned a 429 with a
    ``Retry-After`` header. The job worker uses it to schedule the next
    attempt instead of using the default exponential backoff."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ThreadPost:
    """The result of posting a parent RCA. Caller stores `thread_ts` if
    they want to attribute future thread events to this report."""

    channel: str
    thread_ts: str
    parent_message_ts: str
    alt_message_ts: Optional[str]


class SlackTransport(Protocol):
    """Subset of ``slack_sdk.web.WebClient`` we depend on. Letting callers
    pass any compatible object keeps this module testable without a real SDK."""

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        ...


# --- default httpx-based transport --------------------------------------


class HttpxSlackTransport:
    """Minimal ``chat.postMessage`` client over httpx.

    Avoids pulling in ``slack_sdk`` for a single endpoint. The official SDK
    is the right choice once we add reactions, conversations.replies, etc.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        api_base: str = SLACK_API_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not bot_token:
            raise SlackSendError("Slack bot token is required")
        self._bot_token = bot_token
        self._api_base = api_base
        self._timeout = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        url = f"{self._api_base}/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self._bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            resp = self._client.post(url, headers=headers, json=kwargs, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise SlackSendError(f"Slack request failed: {e}") from e
        headers_obj = getattr(resp, "headers", None) or {}
        retry_after_header = headers_obj.get("Retry-After") if hasattr(headers_obj, "get") else None
        if resp.status_code == 429:
            retry_after = _parse_retry_after(retry_after_header)
            raise SlackSendError(
                f"Slack rate limited (429), retry after {retry_after}s",
                retry_after_seconds=retry_after,
            )
        if resp.status_code != 200:
            raise SlackSendError(
                f"Slack returned {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        if not body.get("ok"):
            # Slack body errors (e.g. ratelimited, server_error) can also
            # carry Retry-After in the response headers.
            retry_after = _parse_retry_after(retry_after_header)
            raise SlackSendError(
                f"Slack API error: {body.get('error')!r}",
                retry_after_seconds=retry_after,
            )
        return body


# --- high-level helpers --------------------------------------------------


def post_rca(
    report: RcaReport,
    channel: str,
    *,
    transport: Optional[SlackTransport] = None,
    fallback_text: Optional[str] = None,
) -> ThreadPost:
    """Post the RCA: parent + threaded alternatives, mapping recorded.

    `transport` is a typed handle (``SlackTransport`` Protocol) so tests can
    inject a fake. When omitted, an ``HttpxSlackTransport`` is built from
    ``AI_ONCALL_SLACK_BOT_TOKEN``.
    """
    transport = transport or _default_transport()
    parent_blocks = render_parent(report)
    parent_text = fallback_text or f"RCA for {report.alert.title}"

    parent_resp = transport.chat_postMessage(
        channel=channel,
        text=parent_text,  # accessibility / push notification fallback
        blocks=parent_blocks,
        metadata={
            "event_type": "ai_oncall_rca",
            "event_payload": {
                "report_id": report.report_id,
                "tenant_id": report.tenant_id,
                "alert_id": report.alert.alert_id,
            },
        },
    )
    parent_ts = parent_resp.get("ts")
    if not parent_ts:
        raise SlackSendError("Slack response missing 'ts' on parent message")

    # Persist the mapping so Slack Events API replies on this thread can
    # recover report_id without parsing context blocks.
    record_thread_mapping(
        channel=channel,
        thread_ts=parent_ts,
        report_id=report.report_id,
    )

    alt_blocks = render_alternatives(report)
    alt_ts: Optional[str] = None
    if alt_blocks:
        alt_resp = transport.chat_postMessage(
            channel=channel,
            text="Alternative hypotheses",
            blocks=alt_blocks,
            thread_ts=parent_ts,
        )
        alt_ts = alt_resp.get("ts")

    logger.info(
        "slack_rca_posted",
        extra={
            "channel": channel,
            "thread_ts": parent_ts,
            "report_id": report.report_id,
            "had_alternatives": bool(alt_blocks),
        },
    )
    return ThreadPost(
        channel=channel,
        thread_ts=parent_ts,
        parent_message_ts=parent_ts,
        alt_message_ts=alt_ts,
    )


def post_thread_reply(
    *,
    channel: str,
    thread_ts: str,
    blocks: list[dict[str, Any]],
    text: str = "",
    transport: Optional[SlackTransport] = None,
) -> str:
    """Post a Block Kit message into an existing thread. Returns the new ts."""
    transport = transport or _default_transport()
    resp = transport.chat_postMessage(
        channel=channel,
        text=text or "Thread reply",
        blocks=blocks,
        thread_ts=thread_ts,
    )
    ts = resp.get("ts", "")
    logger.info(
        "slack_thread_reply_posted",
        extra={"channel": channel, "thread_ts": thread_ts, "reply_ts": ts},
    )
    return ts


# --- internals -----------------------------------------------------------


def _parse_retry_after(header: str | None) -> float | None:
    """Slack sends Retry-After as integer seconds (per RFC 7231 §7.1.3).
    A small number of clients send HTTP dates; we accept either."""
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import timezone

        dt = parsedate_to_datetime(header)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from datetime import datetime as _dt

        delta = (dt - _dt.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _default_transport() -> SlackTransport:
    token = getattr(settings, "slack_bot_token", None)
    if not token:
        raise SlackSendError(
            "AI_ONCALL_SLACK_BOT_TOKEN is not set; cannot post to Slack"
        )
    return HttpxSlackTransport(token)
