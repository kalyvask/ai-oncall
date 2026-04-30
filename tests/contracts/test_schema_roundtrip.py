"""Contract tests: every shipped fixture validates against its JSON Schema and
round-trips cleanly through the Pydantic model. If this file goes red, schemas
and models have drifted apart — fix here before any agent code changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_oncall import models
from ai_oncall.schema_loader import validate

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures"


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize(
    "fixture",
    sorted((FIXTURES / "synthetic_alerts").glob("*.json")),
    ids=lambda p: p.name,
)
def test_alert_fixture_matches_schema_and_model(fixture: Path) -> None:
    payload = _read(fixture)
    validate("alert", payload)
    parsed = models.Alert.model_validate(payload)
    assert parsed.tenant_id == payload["tenant_id"]


@pytest.mark.parametrize(
    "fixture",
    sorted((FIXTURES / "expected_reports").glob("*.json")),
    ids=lambda p: p.name,
)
def test_rca_report_fixture_matches_schema_and_model(fixture: Path) -> None:
    payload = _read(fixture)
    validate("rca_report", payload)
    parsed = models.RcaReport.model_validate(payload)
    assert parsed.tenant_id == payload["tenant_id"]
    assert 1 <= len(parsed.hypotheses) <= 5
    top = parsed.hypotheses[0]
    assert top.confidence >= parsed.hypotheses[-1].confidence, "hypotheses must be ranked best-first"


def test_all_schemas_load() -> None:
    """Every schema file under schemas/ parses as a valid Draft 2020-12 schema."""
    from ai_oncall.schema_loader import SCHEMA_DIR, validator_for

    for path in SCHEMA_DIR.glob("*.json"):
        validator_for(path.stem)  # constructs and caches; raises on bad schema
