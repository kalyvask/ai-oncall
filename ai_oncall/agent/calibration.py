"""Stage 5b — calibrated abstention.

The LLM in `synthesize` will happily emit confident hypotheses even when the
evidence shouldn't support them. Calibration is a deterministic post-pass
that overrides the escalation flag (and optionally caps the top
confidence) when objective signals say the agent shouldn't pretend to know.

The four rules:

1. **Cold-start.** No deploy in the last 24h on the alleged root-cause
   service AND no past incident with a matching root_cause_class for the
   alerting service. The agent has no anchor.

2. **Confidence floor.** Top hypothesis confidence below the floor
   (default 0.45). Anything below is statistically a coin flip; surfacing
   it as the verdict is dishonest.

3. **Budget exhausted, low convergence.** All tool calls used AND the top
   hypothesis still under 0.70. The investigation didn't converge.

4. **Two strong leads.** Top two hypotheses both above 0.60. The agent is
   uncertain between competing explanations; an unresolved tie is itself
   information the human needs.

Each rule produces an ``AbstentionReason`` (struct, not free text). The
final ``CalibrationResult`` records all triggered rules so the eval can
grade abstention precision per rule, and the Slack renderer can show a
specific reason instead of generic "low confidence."

All inputs are read off the report and a few side-channels (recent deploys,
past incidents). No LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ai_oncall.models import (
    AbstentionRecord,
    Calibration as CalibrationModel,
    Escalation,
    Hypothesis,
    RcaReport,
)


# --- defaults --------------------------------------------------------------

DEFAULT_CONFIDENCE_FLOOR = 0.45
DEFAULT_LOW_CONVERGENCE_FLOOR = 0.70
DEFAULT_TWO_LEADS_FLOOR = 0.60
DEFAULT_MAX_TOOL_CALLS = 8  # mirrors agent.tools.MAX_TOOL_CALLS_PER_INCIDENT


# --- result types ----------------------------------------------------------


@dataclass(frozen=True)
class AbstentionReason:
    """One triggered rule."""

    code: str
    description: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibrationResult:
    abstain: bool
    reasons: tuple[AbstentionReason, ...]
    top_confidence_cap: Optional[float] = None  # cap applied to top hypothesis if set

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)


# --- rules -----------------------------------------------------------------


def _cold_start(
    *,
    top: Hypothesis,
    recent_deploys: list[dict],
    past_incidents: list[dict],
    now: datetime,
) -> Optional[AbstentionReason]:
    """No deploy in 24h on the implicated service AND no matching past incident."""
    cutoff = now - timedelta(hours=24)
    has_recent_deploy = any(
        _parse_dt(d.get("timestamp")) and _parse_dt(d["timestamp"]) >= cutoff
        for d in recent_deploys
        if d.get("service") == top.root_cause_service or not d.get("service")
    )
    if has_recent_deploy:
        return None

    matched_class = None
    for inc in past_incidents:
        if inc.get("root_cause_service") == top.root_cause_service:
            matched_class = inc.get("root_cause_class")
            break
    if matched_class is not None:
        return None

    return AbstentionReason(
        code="cold_start",
        description=(
            "No deploy in the last 24h on the implicated service and no "
            "matching past incident for this alerting service. The agent "
            "has no anchor to validate the top hypothesis."
        ),
        detail={
            "root_cause_service": top.root_cause_service,
            "deploys_checked": len(recent_deploys),
            "past_incidents_checked": len(past_incidents),
        },
    )


def _confidence_floor(*, top: Hypothesis, floor: float) -> Optional[AbstentionReason]:
    if top.confidence >= floor:
        return None
    return AbstentionReason(
        code="confidence_floor",
        description=(
            f"Top hypothesis confidence {top.confidence:.2f} is below the "
            f"abstention floor {floor:.2f}. Surfacing this as a verdict "
            "would be dishonest."
        ),
        detail={"top_confidence": top.confidence, "floor": floor},
    )


def _budget_exhausted(
    *,
    top: Hypothesis,
    tool_calls_used: int,
    max_tool_calls: int,
    convergence_floor: float,
) -> Optional[AbstentionReason]:
    if tool_calls_used < max_tool_calls:
        return None
    if top.confidence >= convergence_floor:
        return None
    return AbstentionReason(
        code="budget_exhausted",
        description=(
            f"Investigation used all {max_tool_calls} tool calls but the "
            f"top hypothesis only reached {top.confidence:.2f} (need "
            f">= {convergence_floor:.2f} for convergence)."
        ),
        detail={
            "tool_calls_used": tool_calls_used,
            "max_tool_calls": max_tool_calls,
            "top_confidence": top.confidence,
            "convergence_floor": convergence_floor,
        },
    )


def _two_strong_leads(*, hypotheses: list[Hypothesis], floor: float) -> Optional[AbstentionReason]:
    if len(hypotheses) < 2:
        return None
    sorted_h = sorted(hypotheses, key=lambda h: -h.confidence)
    top, second = sorted_h[0], sorted_h[1]
    if top.confidence < floor or second.confidence < floor:
        return None
    if top.root_cause_service == second.root_cause_service:
        return None  # Same service competing on phrasing — not a true tie.
    return AbstentionReason(
        code="two_strong_leads",
        description=(
            f"Two hypotheses cross the {floor:.2f} confidence floor on "
            f"different services ({top.root_cause_service} @ "
            f"{top.confidence:.2f}, {second.root_cause_service} @ "
            f"{second.confidence:.2f}). An unresolved tie is itself a "
            "signal the human needs."
        ),
        detail={
            "first": {
                "service": top.root_cause_service,
                "confidence": top.confidence,
            },
            "second": {
                "service": second.root_cause_service,
                "confidence": second.confidence,
            },
        },
    )


# --- public entry point ----------------------------------------------------


def calibrate(
    report: RcaReport,
    *,
    recent_deploys: Optional[list[dict]] = None,
    past_incidents: Optional[list[dict]] = None,
    tool_calls_used: Optional[int] = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    convergence_floor: float = DEFAULT_LOW_CONVERGENCE_FLOOR,
    two_leads_floor: float = DEFAULT_TWO_LEADS_FLOOR,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    now: Optional[datetime] = None,
) -> tuple[RcaReport, CalibrationResult]:
    """Run the four rules, return (possibly mutated report, calibration result).

    The mutation is narrow:
    - if any rule fires, ``report.escalation`` is forced to should_escalate=True
      and the reason is the joined codes/descriptions.
    - the top hypothesis confidence is capped at min(actual, 0.40) when
      cold_start or budget_exhausted fires (visual signal in Slack/HTML).
    """
    now = now or datetime.now(timezone.utc)
    top = report.hypotheses[0]
    recent_deploys = recent_deploys or []
    past_incidents = past_incidents or []
    # If the caller didn't provide tool count, read it off the report.
    if tool_calls_used is None and report.investigation is not None:
        tool_calls_used = len(report.investigation.tool_calls)
    tool_calls_used = tool_calls_used or 0

    triggered: list[AbstentionReason] = []
    for r in (
        _cold_start(
            top=top,
            recent_deploys=recent_deploys,
            past_incidents=past_incidents,
            now=now,
        ),
        _confidence_floor(top=top, floor=confidence_floor),
        _budget_exhausted(
            top=top,
            tool_calls_used=tool_calls_used,
            max_tool_calls=max_tool_calls,
            convergence_floor=convergence_floor,
        ),
        _two_strong_leads(hypotheses=report.hypotheses, floor=two_leads_floor),
    ):
        if r is not None:
            triggered.append(r)

    if not triggered:
        # Calibration agrees with the LLM. Attach an "accepted" record so
        # downstream consumers (UI, eval) can tell calibration ran and chose
        # not to abstain — vs. the (older) case where calibration was skipped.
        accepted = CalibrationModel(abstain=False, reasons=[], top_confidence_cap=None)
        return report.model_copy(update={"calibration": accepted}), CalibrationResult(
            abstain=False, reasons=()
        )

    cap = None
    if any(r.code in ("cold_start", "budget_exhausted") for r in triggered):
        cap = min(top.confidence, 0.40)

    description = " | ".join(r.description for r in triggered)
    new_escalation = Escalation(should_escalate=True, reason=description)
    structured = CalibrationModel(
        abstain=True,
        reasons=[
            AbstentionRecord(code=r.code, description=r.description, detail=dict(r.detail))
            for r in triggered
        ],
        top_confidence_cap=cap,
    )
    updates: dict[str, object] = {"escalation": new_escalation, "calibration": structured}

    if cap is not None and top.confidence > cap:
        new_top = top.model_copy(update={"confidence": cap})
        new_hypotheses = [new_top, *report.hypotheses[1:]]
        updates["hypotheses"] = new_hypotheses

    return report.model_copy(update=updates), CalibrationResult(
        abstain=True,
        reasons=tuple(triggered),
        top_confidence_cap=cap,
    )


def _parse_dt(value: object) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
