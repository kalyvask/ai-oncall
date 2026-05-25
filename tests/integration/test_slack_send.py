"""Slack chat.postMessage transport.

Uses a fake transport object that conforms to the ``SlackTransport``
Protocol so we don't need network access. Verifies:

- Parent + alternatives get posted in the same thread.
- The (channel, thread_ts) -> report_id mapping is persisted.
- post_thread_reply round-trips.
- SlackSendError is raised on transport failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ai_oncall.learnings.incidents as incidents_module
from ai_oncall.delivery.send import (
    HttpxSlackTransport,
    SlackSendError,
    post_rca,
    post_thread_reply,
)
from ai_oncall.learnings.incidents import (
    lookup_report_id_by_thread,
    save_incident,
)
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
    return tmp_path


class _FakeTransport:
    """Records every chat_postMessage call and returns a fixture ts."""

    def __init__(self, *, fail: bool = False, ok: bool = True) -> None:
        self.fail = fail
        self.ok = ok
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise SlackSendError("fake transport failure")
        self._counter += 1
        return {"ok": self.ok, "ts": f"170000000.0000{self._counter:02d}"}


def test_post_rca_posts_parent_and_alternatives_in_same_thread(tmp_incidents_db):
    transport = _FakeTransport()
    report = _report()
    save_incident(report)

    result = post_rca(report, "C123", transport=transport)

    # Two calls: parent + alternatives reply.
    assert len(transport.calls) == 2
    parent_call, alt_call = transport.calls
    assert parent_call["channel"] == "C123"
    assert "blocks" in parent_call
    # Parent must NOT have thread_ts set (it's the start of the thread).
    assert "thread_ts" not in parent_call
    # The alternatives message threads off the parent.
    assert alt_call["thread_ts"] == result.thread_ts


def test_post_rca_persists_thread_mapping(tmp_incidents_db):
    transport = _FakeTransport()
    report = _report()
    save_incident(report)

    result = post_rca(report, "C123", transport=transport)

    looked_up = lookup_report_id_by_thread(channel="C123", thread_ts=result.thread_ts)
    assert looked_up == report.report_id


def test_post_rca_embeds_metadata_for_slack_clients(tmp_incidents_db):
    """Slack's ``message.metadata`` is one of the surfaces the Events API
    forwards to thread replies. We embed report_id there too."""
    transport = _FakeTransport()
    report = _report()
    save_incident(report)

    post_rca(report, "C123", transport=transport)

    parent_call = transport.calls[0]
    md = parent_call.get("metadata") or {}
    assert md.get("event_type") == "ai_oncall_rca"
    assert md["event_payload"]["report_id"] == report.report_id


def test_post_rca_skips_alternatives_when_only_one_hypothesis(tmp_incidents_db):
    transport = _FakeTransport()
    report = _report()
    only_top = report.model_copy(update={"hypotheses": report.hypotheses[:1]})

    post_rca(only_top, "C123", transport=transport)

    # Only the parent message is posted.
    assert len(transport.calls) == 1


def test_post_rca_raises_when_parent_response_lacks_ts(tmp_incidents_db):
    class _NoTsTransport(_FakeTransport):
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"ok": True}  # missing 'ts'

    report = _report()
    with pytest.raises(SlackSendError):
        post_rca(report, "C123", transport=_NoTsTransport())


def test_post_thread_reply_round_trip(tmp_incidents_db):
    transport = _FakeTransport()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]
    ts = post_thread_reply(channel="C1", thread_ts="170.001", blocks=blocks, transport=transport)
    assert ts.startswith("17000")
    assert transport.calls[0]["thread_ts"] == "170.001"
    assert transport.calls[0]["blocks"] == blocks


# --- httpx-based transport (no network) ----------------------------------


def test_httpx_transport_raises_on_non_200(monkeypatch):
    """End-to-end of HttpxSlackTransport when Slack returns 5xx."""

    transport = HttpxSlackTransport("xoxb-test")

    class _Resp:
        status_code = 502
        text = "bad gateway"

        def json(self):
            return {}

    def fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr(transport._client, "post", fake_post)
    with pytest.raises(SlackSendError):
        transport.chat_postMessage(channel="C1", text="hi")


def test_httpx_transport_raises_on_ok_false(monkeypatch):
    """Slack returns 200 but with ``ok: false`` -> error."""
    transport = HttpxSlackTransport("xoxb-test")

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": False, "error": "channel_not_found"}

    monkeypatch.setattr(transport._client, "post", lambda *a, **k: _Resp())
    with pytest.raises(SlackSendError):
        transport.chat_postMessage(channel="C1", text="hi")


def test_httpx_transport_rejects_empty_token():
    with pytest.raises(SlackSendError):
        HttpxSlackTransport("")
