"""Stage 6 (POST) + stage 7 (LEARN) end-to-end.

Slack rendering is a pure function so the test asserts block shape.
The learnings store is append-only JSONL; we round-trip a report and
verify retrieve_similar returns it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_oncall.delivery.html import render as render_html
from ai_oncall.delivery.slack import render_alternatives, render_parent
from ai_oncall.learnings import store as learnings
from ai_oncall.models import RcaReport

REPO = Path(__file__).resolve().parents[2]


def _report() -> RcaReport:
    payload = json.loads(
        (REPO / "fixtures/expected_reports/checkout_regression.json").read_text(encoding="utf-8")
    )
    return RcaReport.model_validate(payload)


def test_slack_parent_has_required_blocks() -> None:
    blocks = render_parent(_report())
    types = [b["type"] for b in blocks]
    assert types[0] == "header"
    assert "divider" in types
    # At least one section with the recommended action verbatim.
    actions = [
        b
        for b in blocks
        if b.get("type") == "section" and "Recommended" in b.get("text", {}).get("text", "")
    ]
    assert actions, "parent block must surface the recommended action"


def test_slack_alternatives_includes_each_extra_hypothesis() -> None:
    report = _report()
    alts = render_alternatives(report)
    section_blocks = [b for b in alts if b.get("type") == "section"]
    # Each alt hypothesis renders header+reasoning+evidence+recommended (>=3 sections).
    assert len(section_blocks) >= 3 * (len(report.hypotheses) - 1)


def test_html_export_has_all_hypotheses() -> None:
    report = _report()
    html = render_html(report)
    assert "<!doctype html>" in html
    for h in report.hypotheses:
        assert h.root_cause_service in html
    # OKLCH tokens must be present per UI_DESIGN.md.
    assert "oklch(" in html


def test_learnings_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(learnings, "LEARNINGS_PATH", tmp_path / "learnings.jsonl")
    report = _report()
    record = learnings.append_from_report(report, reaction="thumbs_up")
    assert record.top_hypothesis == "payment"

    matches = learnings.retrieve_similar(
        tenant_id=report.tenant_id,
        alert_title="checkout p99 latency 2.18s (threshold 1.5s) for 5 min",
    )
    assert len(matches) == 1
    assert matches[0].top_hypothesis == "payment"
    # Tenant isolation: a different tenant gets nothing.
    assert learnings.retrieve_similar(tenant_id="other", alert_title="checkout p99 latency") == []
