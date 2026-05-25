"""Convert negative reactions into eval regression fixtures.

Every 👎 / "wrong root cause" reaction (whether from a Slack reaction or a
``mark_wrong_root_cause`` button click) lands in ``learnings.jsonl``. Those
records are useful as a real-time correction signal, but the eval harness
expects ``evals/cases/*.json`` files with a specific shape. This module
bridges the two: read the negative reactions, look up the original RCA in
``data/incidents.sqlite``, and emit one JSON case per failure.

The emitted shape (matches the synthetic cases the harness already grades):

::

    {
        "case_id": "<derived from report_id>",
        "alert": <full Alert payload>,
        "expected": {
            "wrong_root_cause_service": "<what the agent said>",
            "user_label": "wrong_root_cause" | "thumbs_down",
            "correction": "<free-text correction, if any>"
        },
        "source": "feedback_loop:<recorded_at>"
    }

Important: a case here is a *negative* fixture. The eval harness treats it
as a regression test — the agent should NOT predict ``wrong_root_cause_service``
again on the same alert. Pair this with explicit ``expected.root_cause`` once
the human supplies a correction; until then, the case asserts only that the
agent doesn't repeat its prior mistake.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ai_oncall.learnings.incidents import get_incident
from ai_oncall.learnings.store import LEARNINGS_PATH, LearningRecord, Reaction

logger = logging.getLogger(__name__)


NEGATIVE_REACTIONS: tuple[Reaction, ...] = ("thumbs_down", "wrong_root_cause")


@dataclass(frozen=True)
class FeedbackCase:
    """One regression-test case derived from a negative reaction."""

    case_id: str
    report_id: str
    record: LearningRecord
    correction: Optional[str]
    payload: dict


def iter_negative_records(
    *,
    learnings_path: Optional[Path] = None,
    tenant_id: Optional[str] = None,
) -> Iterator[LearningRecord]:
    """Yield each negative reaction in the order it was recorded."""
    path = learnings_path or LEARNINGS_PATH
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = LearningRecord.model_validate_json(line)
            except Exception:
                # Malformed line; skip rather than fail the whole export.
                # The audit-row entries written by reactions.py are not
                # LearningRecord-shaped, so they fall through harmlessly.
                continue
            if record.reaction not in NEGATIVE_REACTIONS:
                continue
            if tenant_id and record.tenant_id != tenant_id:
                continue
            yield record


def build_case(record: LearningRecord) -> Optional[FeedbackCase]:
    """Build a FeedbackCase from one record. Returns None if we can't find
    the originating incident (e.g., it was pruned or never persisted)."""
    incident = get_incident(record.report_id)
    if incident is None:
        logger.warning(
            "feedback_loop_incident_missing",
            extra={"report_id": record.report_id, "tenant_id": record.tenant_id},
        )
        return None

    report = incident.report()
    case_id = f"feedback_{record.report_id}"
    payload = {
        "case_id": case_id,
        "alert": report.alert.model_dump(mode="json"),
        "expected": {
            "wrong_root_cause_service": report.hypotheses[0].root_cause_service,
            "user_label": record.reaction,
            "correction": record.correction,
        },
        "source": f"feedback_loop:{record.recorded_at.isoformat()}",
    }
    return FeedbackCase(
        case_id=case_id,
        report_id=record.report_id,
        record=record,
        correction=record.correction,
        payload=payload,
    )


def export_cases(
    output_dir: Path,
    *,
    learnings_path: Optional[Path] = None,
    tenant_id: Optional[str] = None,
    overwrite: bool = False,
) -> list[FeedbackCase]:
    """Walk negative reactions and write one JSON case per row.

    Returns the list of cases actually written (excludes duplicates and
    cases whose originating incident is missing).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written: list[FeedbackCase] = []

    for record in iter_negative_records(learnings_path=learnings_path, tenant_id=tenant_id):
        # One case per (report_id, reaction). If the user reacted twice with
        # the same negative label, the later one wins.
        dedupe_key = f"{record.report_id}:{record.reaction}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        case = build_case(record)
        if case is None:
            continue

        target = output_dir / f"{case.case_id}.json"
        if target.exists() and not overwrite:
            continue
        target.write_text(
            json.dumps(case.payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        written.append(case)

    logger.info(
        "feedback_loop_export_complete",
        extra={"written": len(written), "output_dir": str(output_dir)},
    )
    return written
