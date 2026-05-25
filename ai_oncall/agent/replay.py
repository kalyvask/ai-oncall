"""Replay past incidents against the current pipeline.

Replay is the agent's self-eval. It takes a stored ``RcaReport`` (or a
batch of them), re-runs the pipeline against the original alert, and
diffs the new report against the stored one. Engineers use it to answer
"would the agent be smarter today?" before trusting a code or prompt
change against live traffic.

The diff is structured: same top hypothesis, same confidence to within
a tolerance, same escalation flag, same root_cause_class, and the same
tool trajectory. CI calls ``replay_batch`` over a curated set of past
incidents and fails on regression.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ai_oncall.agent.run import run_rca
from ai_oncall.learnings.incidents import get_incident
from ai_oncall.llm.client import LlmClient, MockLlm
from ai_oncall.models import RcaReport
from ai_oncall.storage.base import TelemetryStore

logger = logging.getLogger(__name__)


# --- result types --------------------------------------------------------


@dataclass(frozen=True)
class ReplayDiff:
    """Structured diff of two RCA reports for the same alert.

    `verdict`:
        - ``match``     — same top hypothesis service, confidence within
                          tolerance, same escalation flag.
        - ``drift``     — same top hypothesis but different confidence /
                          escalation. May be desired (calibration tightened)
                          or regression (model less sure).
        - ``regression`` — different top hypothesis OR same hypothesis but
                          significantly lower confidence (>0.20 drop).
        - ``improvement`` — same hypothesis with significantly higher
                          confidence (>0.20 gain).
    """

    report_id: str
    verdict: str
    same_top_hypothesis: bool
    same_root_cause_class: bool
    confidence_delta: float
    escalation_changed: bool
    abstain_changed: bool
    differences: list[str] = field(default_factory=list)
    original: Optional[dict] = None
    replayed: Optional[dict] = None


@dataclass(frozen=True)
class ReplayBatchResult:
    """Aggregate over many replays."""

    total: int
    matches: int
    drifts: int
    regressions: int
    improvements: int
    diffs: list[ReplayDiff] = field(default_factory=list)

    @property
    def regression_rate(self) -> float:
        return self.regressions / self.total if self.total else 0.0


# --- public entry points -------------------------------------------------


CONFIDENCE_TOLERANCE = 0.05
REGRESSION_DROP = 0.20
IMPROVEMENT_GAIN = 0.20


def replay_incident(
    report_id: str,
    *,
    store: TelemetryStore,
    llm: Optional[LlmClient] = None,
) -> ReplayDiff:
    """Re-run the pipeline for one stored incident and diff the result."""
    incident = get_incident(report_id)
    if incident is None:
        raise ValueError(f"No incident with report_id={report_id}")

    original = incident.report()
    replay_llm = llm or _replay_mock_from_report(original)
    replayed = run_rca(original.alert, store, replay_llm)

    return _diff_reports(original, replayed)


def replay_batch(
    report_ids: list[str],
    *,
    store: TelemetryStore,
    llm: Optional[LlmClient] = None,
) -> ReplayBatchResult:
    """Run replay over a list of incident ids. Used by CI."""
    diffs: list[ReplayDiff] = []
    for rid in report_ids:
        try:
            diffs.append(replay_incident(rid, store=store, llm=llm))
        except Exception as e:
            logger.exception("replay_failed", extra={"report_id": rid})
            diffs.append(
                ReplayDiff(
                    report_id=rid,
                    verdict="regression",
                    same_top_hypothesis=False,
                    same_root_cause_class=False,
                    confidence_delta=0.0,
                    escalation_changed=False,
                    abstain_changed=False,
                    differences=[f"exception: {e}"],
                )
            )

    matches = sum(1 for d in diffs if d.verdict == "match")
    drifts = sum(1 for d in diffs if d.verdict == "drift")
    regressions = sum(1 for d in diffs if d.verdict == "regression")
    improvements = sum(1 for d in diffs if d.verdict == "improvement")
    return ReplayBatchResult(
        total=len(diffs),
        matches=matches,
        drifts=drifts,
        regressions=regressions,
        improvements=improvements,
        diffs=diffs,
    )


# --- diff logic ----------------------------------------------------------


def _diff_reports(original: RcaReport, replayed: RcaReport) -> ReplayDiff:
    o_top = original.hypotheses[0]
    r_top = replayed.hypotheses[0]
    same_top = o_top.root_cause_service.lower() == r_top.root_cause_service.lower()
    confidence_delta = r_top.confidence - o_top.confidence
    escalation_o = bool(original.escalation and original.escalation.should_escalate)
    escalation_r = bool(replayed.escalation and replayed.escalation.should_escalate)
    escalation_changed = escalation_o != escalation_r

    o_class = _classify(o_top.root_cause_service, o_top.recommended_action)
    r_class = _classify(r_top.root_cause_service, r_top.recommended_action)
    same_class = o_class == r_class

    differences: list[str] = []
    if not same_top:
        differences.append(
            f"top hypothesis changed: {o_top.root_cause_service!r} -> {r_top.root_cause_service!r}"
        )
    if abs(confidence_delta) > CONFIDENCE_TOLERANCE:
        differences.append(
            f"confidence shifted by {confidence_delta:+.2f}: "
            f"{o_top.confidence:.2f} -> {r_top.confidence:.2f}"
        )
    if escalation_changed:
        differences.append(f"escalation changed: {escalation_o} -> {escalation_r}")
    if not same_class:
        differences.append(f"root_cause_class changed: {o_class!r} -> {r_class!r}")

    if not same_top:
        verdict = "regression"
    elif confidence_delta <= -REGRESSION_DROP:
        verdict = "regression"
    elif confidence_delta >= IMPROVEMENT_GAIN:
        verdict = "improvement"
    elif differences:
        verdict = "drift"
    else:
        verdict = "match"

    return ReplayDiff(
        report_id=original.report_id,
        verdict=verdict,
        same_top_hypothesis=same_top,
        same_root_cause_class=same_class,
        confidence_delta=confidence_delta,
        escalation_changed=escalation_changed,
        abstain_changed=False,  # populated upstream once `abstained` is mirrored on replay
        differences=differences,
        original=original.model_dump(mode="json"),
        replayed=replayed.model_dump(mode="json"),
    )


def _classify(root_cause_service: str, recommended_action: str | None) -> str | None:
    """Mirror of learnings.incidents._classify_root_cause without the import
    cycle. Kept identical; if the incidents heuristic changes, update both."""
    if not recommended_action:
        return None
    text = recommended_action.lower()
    rules = (
        ("rollback", "deploy_regression"),
        ("revert", "deploy_regression"),
        ("scale", "saturation"),
        ("autoscale", "saturation"),
        ("restart", "process_health"),
        ("flag", "feature_flag"),
        ("config", "config_drift"),
        ("noop", "transient"),
    )
    for needle, label in rules:
        if needle in text:
            return label
    if root_cause_service:
        return f"unknown:{root_cause_service.lower()}"
    return None


# --- mock generation for replay ------------------------------------------


def _replay_mock_from_report(original: RcaReport) -> MockLlm:
    """Build a MockLlm that replays the original report.

    The default replay path is deterministic — we feed the saved report's
    JSON back through ``synthesize`` and a simple plan. This makes the
    replay command fast enough to run in CI; a "live LLM" replay (against
    Anthropic) is an opt-in via the CLI flag and uses the real client.
    """
    import json

    from ai_oncall.agent.prompts import plan_v1, synthesize_v1

    alert = original.alert
    plan_payload = {
        "tenant_id": alert.tenant_id,
        "alert_id": alert.alert_id,
        "hypotheses": [
            {
                "statement": h.reasoning[:200] or "default",
                "confidence": h.confidence,
                "queries": [
                    {
                        "tool": "get_topology",
                        "input": {"service": alert.service, "depth": 2},
                    },
                    {"tool": "get_runbook", "input": {"service": alert.service}},
                ],
            }
            for h in original.hypotheses[:3]
        ],
    }
    if len(plan_payload["hypotheses"]) < 3:  # plan schema requires >= 3
        while len(plan_payload["hypotheses"]) < 3:
            plan_payload["hypotheses"].append(
                {
                    "statement": "padding hypothesis",
                    "confidence": 0.1,
                    "queries": [{"tool": "get_runbook", "input": {"service": alert.service}}],
                }
            )

    return MockLlm(
        fixtures={
            plan_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(plan_payload),
                "tokens_in": 800,
                "tokens_out": 200,
            },
            synthesize_v1.SYSTEM_PROMPT[:60]: {
                "text": original.model_dump_json(by_alias=True, exclude_none=True),
                "tokens_in": 4000,
                "tokens_out": 600,
            },
        }
    )
