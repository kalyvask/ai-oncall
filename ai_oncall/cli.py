"""Typer CLI entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

# Windows console default encoding is cp1252; RCA output contains UTF-8
# (arrows, emoji, non-ASCII names). Reconfigure once at import time.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ai_oncall import models
from ai_oncall.logging_setup import configure
from ai_oncall.schema_loader import validate

app = typer.Typer(help="ai-oncall — diagnose production incidents with an LLM.")


@app.command()
def schemas() -> None:
    """List the JSON Schemas the contract relies on."""
    configure()
    from ai_oncall.schema_loader import SCHEMA_DIR

    for path in sorted(SCHEMA_DIR.glob("*.json")):
        typer.echo(path.name)


@app.command("validate-fixture")
def validate_fixture(schema: str, path: Path) -> None:
    """Validate a JSON file against one of the named schemas."""
    configure()
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)
    validate(schema, payload)
    typer.echo(f"OK: {path} matches schemas/{schema}.json")


@app.command()
def rca(
    alert_path: Path = typer.Argument(..., help="Path to a JSON alert envelope (e.g. fixtures/synthetic_alerts/checkout_regression.json)"),
    fixture_report: Path | None = typer.Option(
        None, "--fixture-report",
        help="Path to a canned RcaReport JSON; the MockLlm replays it. Use until live LLM keys are wired.",
    ),
) -> None:
    """Run the full agent loop (RECEIVE -> PLAN -> INVESTIGATE -> SYNTHESIZE)."""
    configure()
    _ = models  # imported for downstream type-checker visibility
    from ai_oncall.agent.prompts import plan_v1, synthesize_v1
    from ai_oncall.agent.run import run_rca
    from ai_oncall.ingest.alerts import receive_from_file
    from ai_oncall.llm.client import MockLlm
    from ai_oncall.storage.factory import make_store

    alert = receive_from_file(alert_path)
    if fixture_report is None:
        raise typer.BadParameter(
            "live LLM is not wired yet (BRIEF.md §13 asks before adding it). "
            "Pass --fixture-report to replay a canned report via MockLlm."
        )
    report_text = fixture_report.read_text(encoding="utf-8")
    plan_payload = {
        "tenant_id": alert.tenant_id, "alert_id": alert.alert_id,
        "hypotheses": [
            {"statement": "default", "confidence": 0.5, "queries": [
                {"tool": "get_topology", "input": {"service": alert.service, "depth": 2}},
                {"tool": "get_runbook", "input": {"service": alert.service}},
                {"tool": "get_recent_deploys", "input": {"service": alert.service, "since": alert.fired_at.isoformat()}},
            ]},
            {"statement": "alt_a", "confidence": 0.3, "queries": [
                {"tool": "get_past_incidents", "input": {"service": alert.service, "k": 3}},
            ]},
            {"statement": "alt_b", "confidence": 0.2, "queries": [
                {"tool": "get_runbook", "input": {"service": alert.service}},
            ]},
        ],
    }
    mock = MockLlm(fixtures={
        plan_v1.SYSTEM_PROMPT[:60]: {"text": json.dumps(plan_payload), "tokens_in": 800, "tokens_out": 200},
        synthesize_v1.SYSTEM_PROMPT[:60]: {"text": report_text, "tokens_in": 4000, "tokens_out": 600},
    })
    report = run_rca(alert, make_store(), mock)
    typer.echo(report.model_dump_json(by_alias=True, exclude_none=True, indent=2))


if __name__ == "__main__":
    app()
