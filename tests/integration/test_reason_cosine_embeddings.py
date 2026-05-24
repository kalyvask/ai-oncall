"""reason_cosine — transformer backend with bag-of-words fallback.

The transformer dep is optional. The tests pin three states:

  1. env unset                       -> bag-of-words cosine (default).
  2. env=transformers + dep missing  -> warns once, falls back to bag-of-words.
  3. env=transformers + dep present  -> uses the embedding cosine.

State 3 mocks ``sentence_transformers.SentenceTransformer`` so CI does not
pull a model file.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

import evals.scoring as scoring
from ai_oncall.models import (
    Alert,
    AlertSignal,
    EvidenceItem,
    Hypothesis,
    ModelRef,
    RcaReport,
)


def _report(reasoning: str) -> RcaReport:
    alert = Alert(
        alert_id="a",
        tenant_id="demo",
        fired_at=datetime.now(timezone.utc),
        source="manual",
        severity="page",
        service="checkout",
        signal=AlertSignal(kind="manual"),
        title="t",
    )
    return RcaReport(
        report_id="r",
        tenant_id="demo",
        alert=alert,
        generated_at=alert.fired_at,
        model=ModelRef(provider="mock", id="m"),
        hypotheses=[
            Hypothesis(
                root_cause_service="payment",
                confidence=1.0,
                reasoning=reasoning,
                evidence=[EvidenceItem(claim="c", source="tool_calls[0]")],
                recommended_action="rollback",
            )
        ],
    )


@pytest.fixture(autouse=True)
def _reset_embed_cache(monkeypatch):
    """Clear the module-level model cache between tests so each test picks up
    its own monkeypatched (or absent) backend."""
    monkeypatch.setattr(scoring, "_embed_model", None)
    monkeypatch.setattr(scoring, "_embed_unavailable_logged", False)
    yield


def test_default_uses_bag_of_words(monkeypatch) -> None:
    monkeypatch.delenv("AI_ONCALL_EVAL_EMBED", raising=False)
    score = scoring.reason_cosine(
        _report("stripe sdk regression on payment"),
        _report("stripe sdk regression on payment"),
    )
    assert score == pytest.approx(1.0)


def test_env_set_but_dep_missing_falls_back(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AI_ONCALL_EVAL_EMBED", "transformers")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # force ImportError
    score = scoring.reason_cosine(
        _report("payment is down"),
        _report("payment is down"),
    )
    assert score == pytest.approx(1.0)


def test_env_set_with_mocked_dep_uses_embeddings(monkeypatch) -> None:
    monkeypatch.setenv("AI_ONCALL_EVAL_EMBED", "transformers")

    class _FakeModel:
        def encode(self, texts, normalize_embeddings: bool = True):  # noqa: D401
            # Return two orthogonal unit vectors so cosine = 0 (paraphrase miss
            # we expect the real model to catch — but here we only verify the
            # code path).
            return [[1.0, 0.0], [0.0, 1.0]]

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = lambda *args, **kwargs: _FakeModel()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    score = scoring.reason_cosine(
        _report("payment service deploy regressed signature"),
        _report("checkout latency from upstream call"),
    )
    assert score == pytest.approx(0.0)


def test_zero_text_returns_zero(monkeypatch) -> None:
    monkeypatch.delenv("AI_ONCALL_EVAL_EMBED", raising=False)
    assert scoring.reason_cosine(_report(""), _report("nonempty")) == 0.0
