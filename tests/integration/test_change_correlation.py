"""Code-and-change correlation tests. Item 4.

Covers two layers:
  1. The GitHubClient HTTP shape (httpx.MockTransport).
  2. correlate_changes attaches diff evidence for each hypothesis using
     local-store excerpts first, falling back to the GitHub client.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import pytest

from ai_oncall.agent.correlation import correlate_changes
from ai_oncall.models import (
    Alert,
    AlertSignal,
    ChangeEvent,
    EvidenceItem,
    Hypothesis,
    ModelRef,
    RcaReport,
)
from ai_oncall.storage.github import CommitPatch, GitHubClient
from ai_oncall.storage.sqlite import SqliteStore

T0 = datetime(2026, 4, 25, 2, 0, tzinfo=timezone.utc)
TENANT = "alpha"


# --- 1. GitHubClient HTTP shape -------------------------------------------


def _commit_payload(sha: str, patch_lines: int = 3) -> dict:
    patch_text = "\n".join(f"+ added line {i}" for i in range(patch_lines))
    return {
        "sha": sha,
        "html_url": f"https://github.com/owner/repo/commit/{sha}",
        "commit": {
            "message": "fix: roll back stripe SDK\n\nDetails follow.",
            "author": {"name": "alice"},
        },
        "files": [
            {"filename": "payment/charges.py", "patch": patch_text},
            {"filename": "payment/__init__.py", "patch": "+ tiny"},
        ],
    }


def test_github_client_parses_commit_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        assert path == "/repos/owner/repo/commits/abc1234"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json=_commit_payload("abc1234"))

    transport = httpx.MockTransport(handler)
    client = GitHubClient("owner/repo", token="secret", client=httpx.Client(transport=transport))
    commit = client.fetch_commit_patch("abc1234")
    assert commit is not None
    assert isinstance(commit, CommitPatch)
    assert commit.sha == "abc1234"
    assert commit.title == "fix: roll back stripe SDK"
    assert commit.actor == "alice"
    assert "payment/charges.py" in commit.patch_excerpt
    assert commit.files_changed == ["payment/charges.py", "payment/__init__.py"]


def test_github_client_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(handler)
    client = GitHubClient("owner/repo", client=httpx.Client(transport=transport))
    assert client.fetch_commit_patch("nope") is None


def test_github_client_returns_none_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    transport = httpx.MockTransport(handler)
    client = GitHubClient("owner/repo", client=httpx.Client(transport=transport))
    assert client.fetch_commit_patch("abc") is None


def test_github_client_validates_repo_format() -> None:
    with pytest.raises(ValueError, match="must be 'owner/name'"):
        GitHubClient("just-a-name")


def test_github_client_truncates_excerpt_to_2k() -> None:
    big_patch = "+ x\n" * 10_000
    payload = {
        "sha": "abc",
        "html_url": "https://github.com/o/r/commit/abc",
        "commit": {"message": "big", "author": {"name": "b"}},
        "files": [{"filename": "f.py", "patch": big_patch}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = GitHubClient("owner/repo", client=httpx.Client(transport=httpx.MockTransport(handler)))
    commit = client.fetch_commit_patch("abc")
    assert commit is not None
    assert len(commit.patch_excerpt) == 2048


# --- 2. correlate_changes -------------------------------------------------


def _alert() -> Alert:
    return Alert(
        alert_id="a1",
        tenant_id=TENANT,
        fired_at=T0,
        source="manual",
        severity="page",
        service="checkout",
        signal=AlertSignal(kind="manual"),
        title="checkout slow",
    )


def _report(hypotheses: list[Hypothesis]) -> RcaReport:
    return RcaReport(
        report_id="r1",
        tenant_id=TENANT,
        alert=_alert(),
        generated_at=T0,
        model=ModelRef(provider="mock", id="mock"),
        hypotheses=hypotheses,
    )


def _hypothesis(service: str, evidence_count: int = 1) -> Hypothesis:
    return Hypothesis(
        root_cause_service=service,
        confidence=0.7,
        reasoning="r",
        evidence=[EvidenceItem(claim=f"baseline-{i}", source="x") for i in range(evidence_count)],
        recommended_action="rollback",
    )


def _change(service: str, sha: str = "abc1234", patch: str = "") -> ChangeEvent:
    return ChangeEvent(
        tenant_id=TENANT,
        event_id=sha,
        service=service,
        kind="pr_merged",
        timestamp=T0 - timedelta(hours=1),
        actor="alice",
        title="bump SDK",
        sha=sha,
        patch_excerpt=patch or None,
    )


def test_correlate_uses_local_patch_excerpt(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    # Direct insert via the SQL layer (write_records is for telemetry, not change_events).
    change = _change("payment", patch="--- payment.py\n+ stripe.charges.create(amount=...)")
    store._conn.execute(
        "INSERT INTO change_events (tenant_id, event_id, service, kind, timestamp, actor, title, url, sha, patch_excerpt, files_changed_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            change.tenant_id,
            change.event_id,
            change.service,
            change.kind,
            change.timestamp.isoformat(),
            change.actor,
            change.title,
            change.url,
            change.sha,
            change.patch_excerpt,
            json.dumps(change.files_changed),
        ),
    )
    store._conn.commit()

    report = _report([_hypothesis("payment")])
    out = correlate_changes(report, store, github=None)

    h = out.hypotheses[0]
    assert len(h.evidence) == 2
    assert "payment.py" in h.evidence[-1].source
    assert "Last deploy on payment" in h.evidence[-1].claim


def test_correlate_falls_back_to_github_when_local_excerpt_empty(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    change = _change("payment", sha="def5678", patch="")  # no local excerpt
    store._conn.execute(
        "INSERT INTO change_events (tenant_id, event_id, service, kind, timestamp, actor, title, url, sha, patch_excerpt, files_changed_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            change.tenant_id,
            change.event_id,
            change.service,
            change.kind,
            change.timestamp.isoformat(),
            change.actor,
            change.title,
            change.url,
            change.sha,
            None,
            "[]",
        ),
    )
    store._conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path.endswith("/commits/def5678"):
            return httpx.Response(200, json=_commit_payload("def5678"))
        return httpx.Response(404)

    github = GitHubClient("owner/repo", client=httpx.Client(transport=httpx.MockTransport(handler)))
    report = _report([_hypothesis("payment")])
    out = correlate_changes(report, store, github=github)

    h = out.hypotheses[0]
    assert len(h.evidence) == 2
    assert "payment/charges.py" in h.evidence[-1].source


def test_correlate_no_op_when_no_recent_deploys(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    report = _report([_hypothesis("payment")])
    out = correlate_changes(report, store, github=None)
    assert len(out.hypotheses[0].evidence) == 1  # unchanged


def test_correlate_respects_evidence_max(tmp_path) -> None:
    store = SqliteStore(path=str(tmp_path / "app.sqlite"))
    change = _change("payment", patch="diff content")
    store._conn.execute(
        "INSERT INTO change_events (tenant_id, event_id, service, kind, timestamp, actor, title, url, sha, patch_excerpt, files_changed_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            change.tenant_id,
            change.event_id,
            change.service,
            change.kind,
            change.timestamp.isoformat(),
            change.actor,
            change.title,
            change.url,
            change.sha,
            change.patch_excerpt,
            "[]",
        ),
    )
    store._conn.commit()

    report = _report([_hypothesis("payment", evidence_count=8)])  # already at max
    out = correlate_changes(report, store, github=None)
    assert len(out.hypotheses[0].evidence) == 8  # unchanged


def test_correlate_skips_when_store_does_not_support_deploys(tmp_path) -> None:
    """If recent_deploys raises NotImplementedError (snowflake/live with no
    GitHub-backed delegate), correlation silently skips."""
    from ai_oncall.storage.snowflake import SnowflakeStore

    report = _report([_hypothesis("payment")])
    out = correlate_changes(report, SnowflakeStore(), github=None)
    assert len(out.hypotheses[0].evidence) == 1  # unchanged
