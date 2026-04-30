"""4 eval metrics from BRIEF.md §7. Cosine uses a hash-based fallback when
sentence-transformers is unavailable so CI does not require a model download.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence

from ai_oncall.models import RcaReport, ToolCallRecord


def component_match(predicted: RcaReport, expected: RcaReport) -> float:
    """1.0 if top hypothesis root_cause_service matches case-insensitively, else 0.0."""
    p = predicted.hypotheses[0].root_cause_service.lower().strip()
    e = expected.hypotheses[0].root_cause_service.lower().strip()
    return 1.0 if p == e else 0.0


_TOKEN = re.compile(r"[a-z0-9]+")


def _bag(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(text.lower()))


def reason_cosine(predicted: RcaReport, expected: RcaReport) -> float:
    """Bag-of-words cosine over top-hypothesis reasoning. Threshold 0.5 in CI.

    Real harness substitutes sentence-transformers all-MiniLM-L6-v2 when
    AI_ONCALL_EVAL_EMBED=transformers and the dep is installed.
    """
    p = _bag(predicted.hypotheses[0].reasoning)
    e = _bag(expected.hypotheses[0].reasoning)
    if not p or not e:
        return 0.0
    keys = set(p) | set(e)
    dot = sum(p[k] * e[k] for k in keys)
    np = math.sqrt(sum(v * v for v in p.values()))
    ne = math.sqrt(sum(v * v for v in e.values()))
    return dot / (np * ne) if np and ne else 0.0


def trajectory_score(
    predicted_calls: Sequence[ToolCallRecord], expected_calls: Sequence[ToolCallRecord]
) -> float:
    """0/1/2 rubric: 0 = different tools, 1 = same tools different order, 2 = same order.

    LLM-as-judge swaps in here when AI_ONCALL_EVAL_JUDGE=llm. The deterministic
    fallback is exact for fixture-mode and good enough as a regression canary.
    """
    p_tools = [c.tool for c in predicted_calls]
    e_tools = [c.tool for c in expected_calls]
    if Counter(p_tools) != Counter(e_tools):
        return 0.0
    return 2.0 if p_tools == e_tools else 1.0


def escalation_precision(predicted: RcaReport, expected: RcaReport) -> float:
    """1.0 if escalation flag matches the expected one (precision proxy on the
    binary classifier). Aggregated across the suite for a real precision number.
    """
    p = bool(predicted.escalation and predicted.escalation.should_escalate)
    e = bool(expected.escalation and expected.escalation.should_escalate)
    return 1.0 if p == e else 0.0


def score_all(predicted: RcaReport, expected: RcaReport) -> dict[str, float]:
    pcalls = predicted.investigation.tool_calls if predicted.investigation else []
    ecalls = expected.investigation.tool_calls if expected.investigation else []
    return {
        "component_match": component_match(predicted, expected),
        "reason_cosine": reason_cosine(predicted, expected),
        "trajectory_score": trajectory_score(pcalls, ecalls),
        "escalation_precision": escalation_precision(predicted, expected),
    }
