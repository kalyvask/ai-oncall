"""OpenRCA Bank loader.

Loads OpenRCA's bank of real production incidents into our
``(Alert, RcaReport)`` pair format. The most stringent track in the suite:
real telemetry, real engineer write-ups, real cascades.

Expected on-disk layout (read from upstream docs; no upstream code vendored
per BRIEF.md §12):

    <data_dir>/
      incidents/
        <incident_id>.json     # one JSON per incident (required)
      telemetry/                # optional companion data, not consumed here
        <incident_id>.parquet

Minimum incident JSON shape this loader reads:

    {
      "incident_id": "openrca-2024-0142",
      "service": "checkout",                # labelled root-cause service
      "narrative": "engineer write-up...",  # used as expected reasoning
      "fired_at": "2024-09-12T15:02:11Z",  # optional
      "severity": "page",                   # optional, default page
      "action": "rolled back deploy abc123", # optional human action
      "noise": false,                       # if true, the case is skipped
      "alert_title": "checkout p99 SLO breach",
      "alerting_service": "checkout"        # alerts often fire on a downstream
                                              # symptom, not the root cause
    }

Documented gaps (BRIEF.md §7):

1. OpenRCA assumes static topology snapshots — the live topology builder
   has nothing to do here; the harness pins to topology.yaml.
2. OpenRCA does NOT publish a reference tool-call sequence. trajectory_score
   on this track grades 0/1/2 against an empty reference and will under-
   report. Switch to LLM-as-judge by setting ``AI_ONCALL_EVAL_JUDGE=llm``.
3. Cases flagged ``"noise": true`` are filtered out before scoring so
   escalation_precision is not dragged down by closed-as-not-an-incident
   rows.
4. OpenRCA labels are written by humans and occasionally disagree with the
   actual root cause. Aggregate trends are the signal; spot-check before
   treating any single regression as a real loss.
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

TENANT_ID = "openrca"


def load_cases(data_dir: Path) -> Iterator[tuple[Alert, RcaReport]]:
    """Yield ``(Alert, RcaReport)`` pairs for every non-noise incident.

    Searches for ``*.json`` files under ``<data_dir>/incidents/``; if that
    subdir is absent, falls back to ``<data_dir>`` itself. Malformed JSON or
    cases flagged ``noise`` are skipped with a warning.
    """
    base = Path(data_dir)
    if not base.exists():
        raise FileNotFoundError(f"OpenRCA data dir not found: {base}")
    if not base.is_dir():
        raise NotADirectoryError(f"OpenRCA data dir is not a directory: {base}")

    incidents_dir = base / "incidents" if (base / "incidents").is_dir() else base
    paths = sorted(incidents_dir.glob("*.json"))
    if not paths:
        logger.warning("OpenRCA loader: no incident JSONs in %s", incidents_dir)
        return

    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("OpenRCA loader: skipping %s (%s)", path.name, exc)
            continue

        if payload.get("noise") is True:
            continue

        try:
            yield _build_pair(path.stem, payload)
        except (KeyError, ValueError) as exc:
            logger.warning("OpenRCA loader: skipping %s (%s)", path.name, exc)


def _build_pair(file_stem: str, incident: dict[str, Any]) -> tuple[Alert, RcaReport]:
    service = incident.get("service") or incident.get("root_cause_service")
    if not isinstance(service, str) or not service:
        raise ValueError("incident JSON missing required 'service' field")

    alerting_service = incident.get("alerting_service") or service
    incident_id = incident.get("incident_id") or file_stem
    narrative = (incident.get("narrative") or incident.get("reason") or "").strip()
    action = (incident.get("action") or incident.get("recommended_action") or "(unknown)").strip()

    severity = incident.get("severity", "page")
    if severity not in ("page", "warn", "info"):
        severity = "page"

    fired_at = _parse_dt(incident.get("fired_at")) or datetime.now(timezone.utc)

    alert = Alert(
        alert_id=f"openrca-{incident_id}",
        tenant_id=TENANT_ID,
        fired_at=fired_at,
        source="manual",
        severity=severity,  # type: ignore[arg-type]
        service=alerting_service,
        signal=AlertSignal(kind="manual"),
        title=incident.get("alert_title") or f"OpenRCA incident {incident_id}",
        description=incident.get("alert_description"),
        labels={"benchmark": "openrca", "incident": incident_id},
        expected_focus_service=service,
    )

    expected = RcaReport(
        report_id=f"openrca-{incident_id}",
        tenant_id=TENANT_ID,
        alert=alert,
        generated_at=fired_at,
        model=ModelRef(provider="mock", id="openrca-ground-truth"),
        investigation=None,
        hypotheses=[
            Hypothesis(
                root_cause_service=service,
                root_cause_datetime=fired_at,
                confidence=1.0,
                reasoning=narrative or f"OpenRCA labelled root cause: {service}",
                evidence=[
                    EvidenceItem(
                        claim=narrative or f"labelled root cause {service}",
                        source=f"openrca:{incident_id}",
                    )
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
