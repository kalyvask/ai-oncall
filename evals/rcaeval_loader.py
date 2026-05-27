"""RCAEval RE3-OB loader.

Converts the RE3-OB benchmark layout into our ``(Alert, RcaReport)`` pair
format so the eval harness scores the agent on the same 4 metrics that
the synthetic track uses (component_match, reason_cosine, trajectory_score,
escalation_precision).

Expected on-disk layout (one subdirectory per scenario):

    <data_dir>/
      <scenario_name>/
        gt.json          # ground truth
        metrics.csv      # optional
        logs.csv         # optional
        traces.csv       # optional

Minimum ``gt.json`` shape this loader reads:

    {
      "service": "checkout",                       # root-cause service (required)
      "reason": "stripe SDK signature changed",   # free-text engineer narrative
      "fired_at": "2026-04-25T03:14:22Z",        # optional; falls back to now()
      "severity": "page",                         # optional; defaults to "page"
      "alert_title": "checkout p99 > 2s",          # optional
      "action": "rollback payment to v7"           # optional
    }

Per BRIEF.md §12, the data layout is reimplemented from upstream docs; no
upstream code is vendored. Scenarios with malformed ``gt.json`` are skipped
with a warning rather than raising — the benchmark trends matter more than
any single case.

Documented gaps (BRIEF.md §7):

1. RE3-OB scenarios assume static topology — our live builder is not
   exercised here. The harness pins to topology.yaml when this track runs.
2. RE3-OB does not publish a reference tool-call sequence; trajectory_score
   on this track grades 0/1/2 against an empty reference and will under-
   report. Switch to LLM-as-judge by setting ``AI_ONCALL_EVAL_JUDGE=llm``.
3. Service names in RE3-OB scenarios do not match the synthetic-track
   topology (``payment``, ``checkout``, ...). Cases are namespaced under
   ``tenant_id="rcaeval"`` so they cannot collide with synthetic state.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_oncall.models import (
    Alert,
    AlertSignal,
    EvidenceItem,
    Hypothesis,
    ModelRef,
    RcaReport,
)

logger = logging.getLogger(__name__)

TENANT_ID = "rcaeval"


def load_cases(data_dir: Path) -> Iterator[tuple[Alert, RcaReport]]:
    """Yield ``(Alert, RcaReport)`` pairs for every well-formed scenario.

    Scans ``data_dir`` for immediate subdirectories containing ``gt.json``.
    Subdirectories without ``gt.json``, or with malformed JSON, are skipped
    with a warning.
    """
    base = Path(data_dir)
    if not base.exists():
        raise FileNotFoundError(f"RCAEval data dir not found: {base}")
    if not base.is_dir():
        raise NotADirectoryError(f"RCAEval data dir is not a directory: {base}")

    scenarios = sorted(p for p in base.iterdir() if p.is_dir())
    if not scenarios:
        logger.warning("RCAEval loader: no scenario subdirs in %s", base)
        return

    for scenario in scenarios:
        gt_path = scenario / "gt.json"
        if not gt_path.exists():
            logger.warning("RCAEval loader: skipping %s (no gt.json)", scenario.name)
            continue
        try:
            payload = json.loads(gt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("RCAEval loader: skipping %s (gt.json: %s)", scenario.name, exc)
            continue

        try:
            yield _build_pair(scenario.name, payload)
        except (KeyError, ValueError) as exc:
            logger.warning("RCAEval loader: skipping %s (%s)", scenario.name, exc)


def _build_pair(scenario_name: str, gt: dict[str, Any]) -> tuple[Alert, RcaReport]:
    service = gt.get("service") or gt.get("root_cause_service")
    if not isinstance(service, str) or not service:
        raise ValueError("gt.json missing required 'service' field")

    reason = (gt.get("reason") or gt.get("narrative") or "").strip()
    action = (gt.get("action") or gt.get("recommended_action") or "(unknown)").strip()
    severity = gt.get("severity", "page")
    if severity not in ("page", "warn", "info"):
        severity = "page"

    fired_at = _parse_dt(gt.get("fired_at")) or datetime.now(timezone.utc)

    alert_id = f"rcaeval-{scenario_name}"
    alert = Alert(
        alert_id=alert_id,
        tenant_id=TENANT_ID,
        fired_at=fired_at,
        source="manual",
        severity=severity,  # type: ignore[arg-type]
        service=service,
        signal=AlertSignal(kind="manual"),
        title=gt.get("alert_title") or f"RCAEval scenario {scenario_name}",
        description=gt.get("alert_description"),
        labels={"benchmark": "rcaeval", "scenario": scenario_name},
        expected_focus_service=service,
    )

    expected = RcaReport(
        report_id=f"rcaeval-{scenario_name}",
        tenant_id=TENANT_ID,
        alert=alert,
        generated_at=fired_at,
        model=ModelRef(provider="mock", id="rcaeval-ground-truth"),
        investigation=None,
        hypotheses=[
            Hypothesis(
                root_cause_service=service,
                root_cause_datetime=fired_at,
                confidence=1.0,
                reasoning=reason or f"Injected fault: {scenario_name}",
                evidence=[
                    EvidenceItem(claim=reason or scenario_name, source=f"rcaeval:{scenario_name}")
                ],
                recommended_action=action,
            )
        ],
    )
    return alert, expected


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
