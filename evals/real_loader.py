"""Loader for ``evals/cases/real/*.json`` — the public-postmortem benchmark.

The structure is lighter than the synthetic cases (no full expected RCA
report) because we don't have a deterministic "right answer" for a real
incident's reasoning trace. We score against:

- ``top_root_cause_service``: agent's top hypothesis must match.
- ``root_cause_class``: classified root-cause class (deploy_regression,
  config_drift, saturation, etc.) must match.

Each case carries a ``source_url`` so the ground-truth label is auditable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ai_oncall.models import Alert


@dataclass(frozen=True)
class RealIncidentCase:
    case_id: str
    family: str
    difficulty: str
    source_url: str
    alert: Alert
    expected_top_root_cause_service: str
    expected_root_cause_class: str
    notes: str


_REAL_DIR = Path(__file__).resolve().parent / "cases" / "real"


def iter_real_cases(directory: Path | None = None) -> Iterator[RealIncidentCase]:
    base = directory or _REAL_DIR
    for path in sorted(base.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        yield RealIncidentCase(
            case_id=payload["case_id"],
            family=payload["family"],
            difficulty=payload.get("difficulty", "unknown"),
            source_url=payload["source_url"],
            alert=Alert.model_validate(payload["alert"]),
            expected_top_root_cause_service=payload["expected"]["top_root_cause_service"],
            expected_root_cause_class=payload["expected"]["root_cause_class"],
            notes=payload["expected"].get("notes", ""),
        )


def score_real_case(
    case: RealIncidentCase,
    predicted_top_service: str,
    predicted_root_cause_class: str | None,
) -> dict[str, float]:
    """Two binary scores. Aggregate across the benchmark for accuracy."""
    return {
        "service_match": 1.0
        if predicted_top_service.lower().strip() == case.expected_top_root_cause_service.lower().strip()
        else 0.0,
        "class_match": 1.0
        if (predicted_root_cause_class or "").lower().strip()
        == case.expected_root_cause_class.lower().strip()
        else 0.0,
    }
