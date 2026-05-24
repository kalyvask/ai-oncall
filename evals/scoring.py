"""4 eval metrics from BRIEF.md §7.

``reason_cosine`` uses sentence-transformers ``all-MiniLM-L6-v2`` when
``AI_ONCALL_EVAL_EMBED=transformers`` and the optional dep is installed; the
bag-of-words fallback runs otherwise so CI never requires a model download.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from typing import Sequence

from ai_oncall.models import RcaReport, ToolCallRecord

logger = logging.getLogger(__name__)


def component_match(predicted: RcaReport, expected: RcaReport) -> float:
    """1.0 if top hypothesis root_cause_service matches case-insensitively, else 0.0."""
    p = predicted.hypotheses[0].root_cause_service.lower().strip()
    e = expected.hypotheses[0].root_cause_service.lower().strip()
    return 1.0 if p == e else 0.0


_TOKEN = re.compile(r"[a-z0-9]+")
_EMBED_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
_embed_model: object | None = None
_embed_unavailable_logged = False


def _bag(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(text.lower()))


def _bow_cosine(p_text: str, e_text: str) -> float:
    p = _bag(p_text)
    e = _bag(e_text)
    if not p or not e:
        return 0.0
    keys = set(p) | set(e)
    dot = sum(p[k] * e[k] for k in keys)
    np = math.sqrt(sum(v * v for v in p.values()))
    ne = math.sqrt(sum(v * v for v in e.values()))
    return dot / (np * ne) if np and ne else 0.0


def _load_embed_model() -> object | None:
    """Lazy-load the MiniLM model. Returns None when the dep is absent so
    callers can fall back without crashing."""
    global _embed_model, _embed_unavailable_logged
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError:
        if not _embed_unavailable_logged:
            logger.warning(
                "AI_ONCALL_EVAL_EMBED=transformers requested but "
                "sentence-transformers is not installed; "
                "install ai-oncall[eval-embeddings] to enable. "
                "Falling back to bag-of-words cosine."
            )
            _embed_unavailable_logged = True
        return None
    _embed_model = SentenceTransformer(_EMBED_MODEL_ID)
    return _embed_model


def _embedding_cosine(p_text: str, e_text: str) -> float | None:
    """Return cosine similarity from MiniLM embeddings, or None when the
    backend is unavailable so the caller can fall back."""
    model = _load_embed_model()
    if model is None:
        return None
    vectors = model.encode([p_text, e_text], normalize_embeddings=True)  # type: ignore[attr-defined]
    p_vec, e_vec = vectors[0], vectors[1]
    return float(sum(a * b for a, b in zip(p_vec, e_vec)))


def reason_cosine(predicted: RcaReport, expected: RcaReport) -> float:
    """Cosine similarity over top-hypothesis reasoning. Threshold 0.5 in CI.

    With ``AI_ONCALL_EVAL_EMBED=transformers`` and the optional
    ``[eval-embeddings]`` extra installed, the metric uses
    ``sentence-transformers/all-MiniLM-L6-v2`` embeddings — matches paraphrases
    that the bag-of-words fallback misses. Otherwise the bag-of-words cosine
    runs so CI never blocks on a model download.
    """
    p_text = predicted.hypotheses[0].reasoning
    e_text = expected.hypotheses[0].reasoning
    if not p_text or not e_text:
        return 0.0
    if os.getenv("AI_ONCALL_EVAL_EMBED", "").lower() == "transformers":
        score = _embedding_cosine(p_text, e_text)
        if score is not None:
            return max(0.0, score)
    return _bow_cosine(p_text, e_text)


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


def top_k_accuracy(predicted: RcaReport, expected: RcaReport, *, k: int = 3) -> float:
    """1.0 when the expected root cause appears anywhere in the predicted
    top-k hypotheses' root_cause_service. Lets the agent miss the headline
    but still be useful as long as the right answer is on the screen."""
    target = expected.hypotheses[0].root_cause_service.lower().strip()
    services = [h.root_cause_service.lower().strip() for h in predicted.hypotheses[:k]]
    return 1.0 if target in services else 0.0


_EVIDENCE_SOURCE = re.compile(
    r"^(tool_calls\[\d+\]|sha:[a-f0-9]{7,40}|change_event:[A-Za-z0-9_\-]+|"
    r"deploy:[A-Za-z0-9_\-]+|metric:[A-Za-z0-9_\-]+|log_query:[A-Za-z0-9_\-]+|"
    r"runbook:[A-Za-z0-9_/.\-]+)$"
)


def evidence_precision(predicted: RcaReport) -> float:
    """Fraction of top-hypothesis evidence items whose ``source`` matches a
    known reference shape. Empty evidence -> 0.0.

    The grader is intentionally narrow: even a non-empty free-text source
    that doesn't look like a verifiable handle (tool_calls[i], a SHA, a
    deploy id) counts as a miss. The point is to penalize hand-wavy
    "from the logs" citations that don't pin to a specific record."""
    h = predicted.hypotheses[0] if predicted.hypotheses else None
    if h is None or not h.evidence:
        return 0.0
    hits = sum(1 for e in h.evidence if _EVIDENCE_SOURCE.match((e.source or "").strip()))
    return hits / len(h.evidence)


def abstention_correctness(predicted: RcaReport, expected: RcaReport) -> float:
    """Did calibration match the expected abstention decision?

    The expected case is annotated with ``calibration.abstain``; the
    predicted report carries the same shape after calibrate(). 1.0 on
    match, 0.0 on mismatch. Aggregated across the suite this is the
    abstention precision/recall number the eval card surfaces.
    """
    p = bool(predicted.calibration and predicted.calibration.abstain)
    e = bool(expected.calibration and expected.calibration.abstain)
    return 1.0 if p == e else 0.0


def unsafe_action_rate(predicted: RcaReport) -> float:
    """Fraction of hypotheses whose ``staged_action`` would be policy-
    downgraded. 0.0 is the goal; any non-zero value means the propose/auto
    tier was assigned to a kind outside the allowlist."""
    from ai_oncall.agent.policy import DEFAULT_POLICY, downgrade_unsafe_tier

    if not predicted.hypotheses:
        return 0.0
    bad = 0
    for h in predicted.hypotheses:
        sa = h.staged_action
        if sa is None:
            continue
        new_tier, _ = downgrade_unsafe_tier(sa.kind, sa.tier, policy=DEFAULT_POLICY)
        if new_tier != sa.tier:
            bad += 1
    return bad / len(predicted.hypotheses)


def score_all(predicted: RcaReport, expected: RcaReport) -> dict[str, float]:
    pcalls = predicted.investigation.tool_calls if predicted.investigation else []
    ecalls = expected.investigation.tool_calls if expected.investigation else []
    return {
        "component_match": component_match(predicted, expected),
        "top_3_accuracy": top_k_accuracy(predicted, expected, k=3),
        "reason_cosine": reason_cosine(predicted, expected),
        "trajectory_score": trajectory_score(pcalls, ecalls),
        "escalation_precision": escalation_precision(predicted, expected),
        "evidence_precision": evidence_precision(predicted),
        "abstention_correctness": abstention_correctness(predicted, expected),
        "unsafe_action_rate": unsafe_action_rate(predicted),
    }
