"""Action staging. Roadmap item 5.

Runs as the last step of the pipeline (after CORRELATE). For each hypothesis,
parses the free-text `recommended_action` into a structured kind, then assigns
a trust tier:

  recommend : default. Human runs it.
  propose   : confidence >= PROPOSE_THRESHOLD. Human approves first.
  auto      : confidence >= AUTO_THRESHOLD AND kind is on the auto whitelist
              AND the alert is page-severity. Runtime executes in a sandbox.

The whitelist is intentionally conservative — only `rollback` qualifies for
`auto` today. Other kinds (`restart`, `scale`, `feature_flag`) require a human
in the loop until the eval shows them safe.

Pure function. No I/O. The execution surface (Slack approval button for
`propose`, sandboxed runner for `auto`) is built on top of this and is not
in scope for this commit — staging just decides the tier.
"""

from __future__ import annotations

import re

from ai_oncall.agent.policy import DEFAULT_POLICY, ActionPolicy, downgrade_unsafe_tier
from ai_oncall.models import (
    ActionKind,
    ActionTier,
    Hypothesis,
    RcaReport,
    StagedAction,
)

PROPOSE_THRESHOLD = 0.50
AUTO_THRESHOLD = 0.85


def stage_actions(report: RcaReport, *, policy: ActionPolicy = DEFAULT_POLICY) -> RcaReport:
    is_page = report.alert.severity == "page"
    for hypothesis in report.hypotheses:
        if hypothesis.staged_action is not None:
            # The LLM already returned one. Still policy-check the tier: an
            # LLM-supplied tier outside the allowlist must be downgraded.
            sa = hypothesis.staged_action
            new_tier, reason = downgrade_unsafe_tier(sa.kind, sa.tier, policy=policy)
            if new_tier != sa.tier:
                hypothesis.staged_action = sa.model_copy(
                    update={
                        "tier": new_tier,
                        "rationale": (sa.rationale or "") + f" Policy override: {reason}",
                    }
                )
            continue
        kind = _infer_kind(hypothesis.recommended_action)
        tier = _classify_tier(kind, hypothesis.confidence, is_page=is_page, policy=policy)
        rationale = _rationale(kind, tier, hypothesis)
        hypothesis.staged_action = StagedAction(
            kind=kind,
            service=hypothesis.root_cause_service,
            tier=tier,
            rationale=rationale,
            runbook_ref=hypothesis.runbook_link,
        )
    return report


# --- inference -------------------------------------------------------------


_KIND_PATTERNS: list[tuple[ActionKind, re.Pattern[str]]] = [
    ("rollback", re.compile(r"\b(roll\s*back|revert|rollout\s+undo)\b", re.I)),
    ("restart", re.compile(r"\b(restart|rollout\s+restart|kubectl\s+rollout\s+restart)\b", re.I)),
    ("scale", re.compile(r"\b(scale|kubectl\s+scale|autoscaler|hpa)\b", re.I)),
    ("feature_flag", re.compile(r"\b(feature\s*flag|disable\s+flag|toggle\s+off)\b", re.I)),
    ("noop", re.compile(r"\b(no[-\s]?op|wait[\s-]and[\s-]see|monitor\s+only)\b", re.I)),
]


def _infer_kind(recommended_action: str) -> ActionKind:
    text = recommended_action or ""
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text):
            return kind
    return "manual"


def _classify_tier(
    kind: ActionKind,
    confidence: float,
    *,
    is_page: bool,
    policy: ActionPolicy = DEFAULT_POLICY,
) -> ActionTier:
    if (
        is_page
        and confidence >= AUTO_THRESHOLD
        and policy.can_auto(kind)
    ):
        return "auto"
    if confidence >= PROPOSE_THRESHOLD and policy.can_propose(kind):
        return "propose"
    return "recommend"


def _rationale(kind: ActionKind, tier: ActionTier, hypothesis: Hypothesis) -> str:
    if tier == "auto":
        return (
            f"{kind} is on the auto-execute whitelist and confidence "
            f"{hypothesis.confidence:.2f} >= {AUTO_THRESHOLD:.2f} on a page-severity alert."
        )
    if tier == "propose":
        return (
            f"confidence {hypothesis.confidence:.2f} >= {PROPOSE_THRESHOLD:.2f}; "
            f"awaiting human approval before {kind}."
        )
    return f"confidence {hypothesis.confidence:.2f} below propose threshold; surfacing as advice only."
