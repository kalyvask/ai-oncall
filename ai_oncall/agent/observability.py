"""Per-LLM-call tracing.

Wraps `LlmClient.generate()` and captures one `LlmCallRecord` per round-trip.
The tracer is plumbed through PLAN and SYNTHESIZE; SYNTHESIZE attaches the
accumulated records to `Investigation.llm_calls` on the final report. The
reasoning-trace tab in the UI then renders them next to the tool calls.

No external sink today. An optional export to Langfuse / Helicone /
OTel-LLM is on the roadmap; the contract here (a flat list of records) is
designed to map onto any of those without further refactoring.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from ai_oncall.llm.client import LlmClient
from ai_oncall.llm.registry import CATALOG, estimate_cost
from ai_oncall.models import LlmCallRecord
from ai_oncall.settings import settings


class LlmTracer:
    """Accumulates LlmCallRecord entries across the pipeline. One tracer per
    incident; pass it to plan() and synthesize()."""

    def __init__(self) -> None:
        self.records: list[LlmCallRecord] = []

    def call(
        self,
        llm: LlmClient,
        prompt: str,
        *,
        stage: str,
        prompt_version: str,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        error: str | None = None
        response: dict[str, Any] = {}
        try:
            response = llm.generate(prompt, max_tokens=max_tokens)
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000
            tokens_in = response.get("tokens_in")
            tokens_out = response.get("tokens_out")
            model_id, alias = _resolve_model()
            cost = (
                estimate_cost(alias, tokens_in or 0, tokens_out or 0)
                if alias in CATALOG and (tokens_in is not None or tokens_out is not None)
                else None
            )
            self.records.append(
                LlmCallRecord(
                    stage=stage,  # type: ignore[arg-type]
                    prompt_version=prompt_version,
                    prompt_hash=prompt_hash,
                    model_id=model_id,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    error=error,
                    started_at=started_at,
                )
            )


def _resolve_model() -> tuple[str, str]:
    for alias, spec in CATALOG.items():
        if spec["id"] == settings.rca_model:
            return spec["id"], alias
    return settings.rca_model, "mock"
