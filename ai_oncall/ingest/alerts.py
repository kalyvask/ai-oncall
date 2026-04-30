"""Stage 1 — RECEIVE. Normalize incoming alert envelopes from PagerDuty, Slack,
manual CLI, etc. into our `Alert` Pydantic model. The wire format is anchored
to the JSON Schema in schemas/alert.json; ingest is just a thin parser plus a
trust boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_oncall.models import Alert
from ai_oncall.schema_loader import validate


def receive(payload: dict[str, Any]) -> Alert:
    """Validate and normalize an incoming alert payload into an Alert model.

    Raises jsonschema.ValidationError on shape mismatch; raises pydantic
    ValidationError on type mismatch. Both surface 400-class errors at the
    HTTP layer (see ai_oncall/server.py).
    """
    validate("alert", payload)
    return Alert.model_validate(payload)


def receive_from_file(path: str | Path) -> Alert:
    with Path(path).open(encoding="utf-8") as f:
        payload = json.load(f)
    return receive(payload)
