"""Stage 7 — LEARN. Append-only learnings.jsonl.

Each line is a self-contained record: report_id, tenant_id, the alert summary,
top hypothesis, and the human signal (👍 / 👎 / "wrong root cause" reaction,
or a free-text correction). Retrieval is k-NN over an embedding of the alert
title; the simple LIKE-based fallback below ships with the v1 cut.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ai_oncall.models import RcaReport

LEARNINGS_PATH = Path("data/learnings.jsonl")
Reaction = Literal["thumbs_up", "thumbs_down", "wrong_root_cause"]


class LearningRecord(BaseModel):
    tenant_id: str
    report_id: str
    alert_title: str
    service: str
    top_hypothesis: str
    confidence: float
    reaction: Reaction | None = None
    correction: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def append_from_report(
    report: RcaReport, *, reaction: Reaction | None = None, correction: str | None = None
) -> LearningRecord:
    LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = LearningRecord(
        tenant_id=report.tenant_id,
        report_id=report.report_id,
        alert_title=report.alert.title,
        service=report.alert.service,
        top_hypothesis=report.hypotheses[0].root_cause_service,
        confidence=report.hypotheses[0].confidence,
        reaction=reaction,
        correction=correction,
    )
    with LEARNINGS_PATH.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")
    return record


def retrieve_similar(tenant_id: str, alert_title: str, k: int = 3) -> list[LearningRecord]:
    """LIKE-based fallback retrieval. Real k-NN with sentence-transformers
    lands when the eval shows it materially improves trajectory_score."""
    if not LEARNINGS_PATH.exists():
        return []
    matches: list[LearningRecord] = []
    title_lower = alert_title.lower()
    with LEARNINGS_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                record = LearningRecord.model_validate_json(line)
            except Exception:
                continue
            if record.tenant_id != tenant_id:
                continue
            if any(
                token in record.alert_title.lower()
                for token in title_lower.split()
                if len(token) > 3
            ):
                matches.append(record)
    return matches[:k]
