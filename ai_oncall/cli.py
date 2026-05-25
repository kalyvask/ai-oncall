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
    alert_path: Path = typer.Argument(
        ...,
        help="Path to a JSON alert envelope (e.g. fixtures/synthetic_alerts/checkout_regression.json)",
    ),
    fixture_report: Path | None = typer.Option(
        None,
        "--fixture-report",
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
        "tenant_id": alert.tenant_id,
        "alert_id": alert.alert_id,
        "hypotheses": [
            {
                "statement": "default",
                "confidence": 0.5,
                "queries": [
                    {"tool": "get_topology", "input": {"service": alert.service, "depth": 2}},
                    {"tool": "get_runbook", "input": {"service": alert.service}},
                    {
                        "tool": "get_recent_deploys",
                        "input": {"service": alert.service, "since": alert.fired_at.isoformat()},
                    },
                ],
            },
            {
                "statement": "alt_a",
                "confidence": 0.3,
                "queries": [
                    {"tool": "get_past_incidents", "input": {"service": alert.service, "k": 3}},
                ],
            },
            {
                "statement": "alt_b",
                "confidence": 0.2,
                "queries": [
                    {"tool": "get_runbook", "input": {"service": alert.service}},
                ],
            },
        ],
    }
    mock = MockLlm(
        fixtures={
            plan_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(plan_payload),
                "tokens_in": 800,
                "tokens_out": 200,
            },
            synthesize_v1.SYSTEM_PROMPT[:60]: {
                "text": report_text,
                "tokens_in": 4000,
                "tokens_out": 600,
            },
        }
    )
    report = run_rca(alert, make_store(), mock)
    typer.echo(report.model_dump_json(by_alias=True, exclude_none=True, indent=2))


@app.command()
def replay(
    report_id: str | None = typer.Argument(
        None, help="Stored report id. Omit and pass --batch-from to replay many."
    ),
    batch_from: Path | None = typer.Option(
        None,
        "--batch-from",
        help="Path to a text file with one report_id per line; replays each.",
    ),
    fail_on_regression: bool = typer.Option(
        True,
        "--fail-on-regression/--no-fail-on-regression",
        help="Exit non-zero if any replay regresses (use in CI).",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Re-run the pipeline on a stored incident and diff against the saved report."""
    configure()
    from ai_oncall.agent.replay import replay_batch, replay_incident
    from ai_oncall.storage.factory import make_store

    store = make_store()

    ids: list[str] = []
    if batch_from is not None:
        ids = [
            line.strip()
            for line in batch_from.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    elif report_id:
        ids = [report_id]
    else:
        raise typer.BadParameter("Provide a REPORT_ID or --batch-from <file>")

    if len(ids) == 1:
        diff = replay_incident(ids[0], store=store)
        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "report_id": diff.report_id,
                        "verdict": diff.verdict,
                        "confidence_delta": diff.confidence_delta,
                        "differences": diff.differences,
                    },
                    indent=2,
                )
            )
        else:
            typer.echo(f"\nreplay {diff.report_id}:")
            typer.echo(f"  verdict           : {diff.verdict}")
            typer.echo(f"  same top          : {diff.same_top_hypothesis}")
            typer.echo(f"  same class        : {diff.same_root_cause_class}")
            typer.echo(f"  confidence delta  : {diff.confidence_delta:+.2f}")
            typer.echo(f"  escalation change : {diff.escalation_changed}")
            for line in diff.differences:
                typer.echo(f"   - {line}")
        if fail_on_regression and diff.verdict == "regression":
            raise typer.Exit(1)
        return

    result = replay_batch(ids, store=store)
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "total": result.total,
                    "matches": result.matches,
                    "drifts": result.drifts,
                    "regressions": result.regressions,
                    "improvements": result.improvements,
                    "regression_rate": result.regression_rate,
                    "diffs": [
                        {
                            "report_id": d.report_id,
                            "verdict": d.verdict,
                            "differences": d.differences,
                        }
                        for d in result.diffs
                    ],
                },
                indent=2,
            )
        )
    else:
        typer.echo(f"\nbatch replay over {result.total} incidents:")
        typer.echo(f"  matches      : {result.matches}")
        typer.echo(f"  drifts       : {result.drifts}")
        typer.echo(f"  improvements : {result.improvements}")
        typer.echo(f"  regressions  : {result.regressions}")
        if result.regressions:
            typer.echo("\nregressions:")
            for d in result.diffs:
                if d.verdict == "regression":
                    typer.echo(f"  - {d.report_id}: {'; '.join(d.differences)}")
    if fail_on_regression and result.regressions > 0:
        raise typer.Exit(1)


@app.command()
def feedback_export(
    output_dir: Path = typer.Argument(
        Path("evals/cases/feedback"),
        help="Directory for one JSON case per negative reaction.",
    ),
    tenant_id: str | None = typer.Option(
        None, "--tenant-id", help="Restrict to a single tenant's reactions."
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Re-emit cases that already exist at the destination.",
    ),
) -> None:
    """Export 👎 / wrong-root-cause reactions as eval regression fixtures.

    Each emitted case asserts the agent should NOT predict the same wrong
    root cause again on the same alert. Pair with explicit ``expected.root_cause``
    once a human supplies a correction.
    """
    configure()
    from ai_oncall.learnings.feedback_loop import export_cases

    cases = export_cases(output_dir, tenant_id=tenant_id, overwrite=overwrite)
    typer.echo(f"wrote {len(cases)} fixture cases to {output_dir}")
    for case in cases[:10]:
        typer.echo(
            f"  - {case.case_id}: agent claimed `{case.payload['expected']['wrong_root_cause_service']}`"
        )
    if len(cases) > 10:
        typer.echo(f"  ... and {len(cases) - 10} more")


@app.command()
def promote(
    report_id: str = typer.Argument(..., help="Stored report id to promote."),
    tier: str = typer.Option(
        "verified",
        "--tier",
        help="New trust tier: aggregated (cross-tenant priors) or verified (human-confirmed).",
    ),
) -> None:
    """Promote a stored incident to a higher trust tier.

    Default tier is `verified` (human says "yes, this RCA was right"). Use
    `--tier aggregated` to opt the incident into cross-tenant priors. Both
    tiers must be requested explicitly by callers of `get_past_incidents`.
    """
    configure()
    from ai_oncall.learnings.incidents import promote_incident_tier

    if tier not in {"local", "aggregated", "verified"}:
        raise typer.BadParameter(
            f"--tier must be one of: local, aggregated, verified (got {tier!r})"
        )
    ok = promote_incident_tier(report_id, new_tier=tier)  # type: ignore[arg-type]
    if not ok:
        typer.echo(f"no incident with report_id={report_id}")
        raise typer.Exit(1)
    typer.echo(f"promoted {report_id} -> trust_tier={tier}")


if __name__ == "__main__":
    app()
