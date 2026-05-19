"""Action policy. Centralizes safety rules for the propose / auto tiers.

Three knobs, all read-only:

- ``allow_propose``: kinds that can ever reach the ``propose`` tier (visible
  approval button). Anything outside is forced to ``recommend`` regardless
  of confidence. Default: rollback, restart, feature_flag.

- ``allow_auto``: kinds that can reach ``auto`` (no human in the loop).
  Default: rollback only.

- ``blast_radius_for(kind)``: low / medium / high. The UI uses this to color
  the action layer; the audit log records it. ``manual`` defaults to high
  because a free-text command is the riskiest path.

The policy is intentionally global v1. A per-tenant override surface lands
in P1-9 (auth + tenant config). Until then the safest defaults ship.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ai_oncall.models import ActionKind

BlastRadius = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ActionPolicy:
    allow_propose: frozenset[ActionKind] = field(
        default_factory=lambda: frozenset({"rollback", "restart", "feature_flag"})
    )
    allow_auto: frozenset[ActionKind] = field(default_factory=lambda: frozenset({"rollback"}))
    blast_radius_by_kind: dict[ActionKind, BlastRadius] = field(
        default_factory=lambda: {
            "rollback": "medium",
            "restart": "medium",
            "scale": "medium",
            "feature_flag": "low",
            "manual": "high",
            "noop": "low",
        }
    )

    def blast_radius_for(self, kind: ActionKind) -> BlastRadius:
        return self.blast_radius_by_kind.get(kind, "high")

    def can_propose(self, kind: ActionKind) -> bool:
        return kind in self.allow_propose

    def can_auto(self, kind: ActionKind) -> bool:
        return kind in self.allow_auto


DEFAULT_POLICY = ActionPolicy()


def downgrade_unsafe_tier(
    kind: ActionKind, tier: str, *, policy: ActionPolicy = DEFAULT_POLICY
) -> tuple[str, str | None]:
    """Return (tier, reason). When the kind is not allowed at the requested
    tier, downgrade and return a human-readable reason. ``reason=None``
    means no change."""
    if tier == "auto" and not policy.can_auto(kind):
        if policy.can_propose(kind):
            return "propose", f"kind={kind} is not on the auto whitelist; downgraded to propose."
        return "recommend", f"kind={kind} is not on the auto or propose whitelist; downgraded to recommend."
    if tier == "propose" and not policy.can_propose(kind):
        return "recommend", f"kind={kind} is not on the propose whitelist; downgraded to recommend."
    return tier, None
