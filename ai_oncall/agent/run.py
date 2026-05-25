"""End-to-end orchestrator: RECEIVE -> PLAN -> PRUNE -> INVESTIGATE ->
SYNTHESIZE -> CORRELATE -> STAGE_ACTIONS.

This is the seam the CLI (`ai-oncall rca`) and the FastAPI agent endpoint both
call. Slack delivery (stage 6) wraps the result; the LEARN step (stage 7)
appends to learnings.jsonl and runs out-of-band.

The PRUNE step (item 3) sits between PLAN and INVESTIGATE; it drops
hypotheses whose claimed root cause is unreachable from the alerting service,
freeing the 8-call budget for plausible candidates only.

The CORRELATE step (item 4) sits after SYNTHESIZE; it attaches the most
recent deploy diff for each hypothesis's `root_cause_service` as evidence.

The STAGE_ACTIONS step (item 5) classifies each hypothesis's recommended
action into one of three trust tiers (recommend / propose / auto) for the
delivery surfaces to act on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ai_oncall.agent.calibration import CalibrationResult, calibrate
from ai_oncall.agent.causal import claimed_services, prune_plan
from ai_oncall.agent.correlation import correlate_changes
from ai_oncall.agent.investigate import investigate
from ai_oncall.agent.observability import LlmTracer
from ai_oncall.agent.plan import plan as plan_stage
from ai_oncall.agent.staging import stage_actions
from ai_oncall.agent.synthesize import synthesize
from ai_oncall.agent.tools import get_past_incidents, get_recent_deploys
from ai_oncall.learnings.incidents import save_incident
from ai_oncall.llm.client import LlmClient
from ai_oncall.models import Alert, RcaReport
from ai_oncall.settings import settings
from ai_oncall.storage.base import TelemetryStore
from ai_oncall.storage.github import GitHubClient
from ai_oncall.topology.builder import build as build_topology

logger = logging.getLogger(__name__)


def run_rca(
    alert: Alert,
    store: TelemetryStore,
    llm: LlmClient,
    *,
    skip_slack: bool = False,
) -> RcaReport:
    """Run the full RCA pipeline synchronously.

    ``skip_slack=True`` is set by the job worker, which handles delivery via
    a separate retryable ``slack_post`` job. The CLI keeps the default
    (inline best-effort post) so developer flows stay one-shot.
    """
    tracer = LlmTracer()
    plan_obj = plan_stage(alert, llm, tracer=tracer)
    topology = build_topology(alert.tenant_id, store)
    pruned = prune_plan(plan_obj, alert, topology)
    trace, bundle = investigate(alert.tenant_id, pruned.active, store)
    if pruned.pruned:
        bundle["pruned_hypotheses"] = [
            {
                "statement": p.hypothesis.statement,
                "claimed_services": sorted(claimed_services(p.hypothesis)),
                "reason": p.reason,
            }
            for p in pruned.pruned
        ]
    report = synthesize(alert, context=bundle, llm=llm, tool_calls=trace, tracer=tracer)
    report = correlate_changes(report, store, github=_make_github_client())
    report = stage_actions(report)

    # Calibrated abstention: deterministic post-pass that overrides the LLM's
    # escalation flag when the evidence doesn't actually support the verdict.
    report, calibration = _apply_calibration(report, store)

    # Persist the report so replay + the typed memory graph have something to
    # read back. Failures here must never break the live RCA path; log and
    # continue.
    try:
        save_incident(report, abstained=calibration.abstain)
    except Exception:
        logger.exception("save_incident_failed", extra={"report_id": report.report_id})

    if not skip_slack:
        _maybe_post_to_slack(report)

    return report


def _maybe_post_to_slack(report: RcaReport) -> None:
    if not (settings.slack_bot_token and settings.slack_default_channel):
        return
    try:
        from ai_oncall.delivery.send import SlackSendError, post_rca
    except ImportError:  # pragma: no cover
        return
    try:
        post_rca(report, settings.slack_default_channel)
    except SlackSendError as e:
        logger.warning(
            "slack_post_failed",
            extra={"report_id": report.report_id, "error": str(e)[:200]},
        )


def _apply_calibration(
    report: RcaReport, store: TelemetryStore
) -> tuple[RcaReport, CalibrationResult]:
    """Pull the side-channel signals calibration needs, then run it.

    Past incidents come from the typed memory graph (local tier only at this
    layer; aggregated tier requires explicit caller opt-in). Recent deploys
    come from the same storage the tools use.
    """
    top_root = (
        report.hypotheses[0].root_cause_service if report.hypotheses else report.alert.service
    )
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    try:
        recent_deploys = get_recent_deploys(
            store, report.tenant_id, service=top_root, since=since.isoformat()
        )
    except Exception as e:
        # Calibration without deploy history will probably fire cold_start.
        # That's the safer failure mode (we abstain), but still surface so
        # ops can fix the underlying store.
        logger.warning(
            "calibration_recent_deploys_unavailable",
            extra={
                "tenant_id": report.tenant_id,
                "service": top_root,
                "error_type": type(e).__name__,
                "error": str(e)[:200],
            },
        )
        recent_deploys = []

    try:
        past_incidents_raw = get_past_incidents(
            store, report.tenant_id, service=report.alert.service, k=5
        )
        # Filter out the trailing summary item before passing in.
        past_incidents = [p for p in past_incidents_raw if "_root_cause_class_summary" not in p]
    except Exception as e:
        logger.warning(
            "calibration_past_incidents_unavailable",
            extra={
                "tenant_id": report.tenant_id,
                "service": report.alert.service,
                "error_type": type(e).__name__,
                "error": str(e)[:200],
            },
        )
        past_incidents = []

    new_report, calibration = calibrate(
        report,
        recent_deploys=recent_deploys,
        past_incidents=past_incidents,
    )
    if calibration.abstain:
        logger.info(
            "calibration_triggered_abstention",
            extra={
                "tenant_id": report.tenant_id,
                "service": report.alert.service,
                "report_id": report.report_id,
                "rules": list(calibration.codes),
                "confidence_cap": calibration.top_confidence_cap,
            },
        )
    return new_report, calibration


def _make_github_client() -> GitHubClient | None:
    if not settings.github_repo:
        return None
    return GitHubClient(
        settings.github_repo,
        token=settings.github_token,
        api_url=settings.github_api_url,
    )
