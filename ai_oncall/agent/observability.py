"""Per-LLM-call tracing.

Wraps `LlmClient.generate()` and captures one `LlmCallRecord` per round-trip.
The tracer is plumbed through PLAN and SYNTHESIZE; SYNTHESIZE attaches the
accumulated records to `Investigation.llm_calls` on the final report. The
reasoning-trace tab in the UI then renders them next to the tool calls.

Optional Langfuse export. When ``langfuse_public_key`` AND
``langfuse_secret_key`` are configured, each ``LlmTracer.call`` also POSTs
a span to the Langfuse ingestion endpoint. Failures here never break the
RCA path; they log and continue.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ai_oncall.llm.client import LlmClient
from ai_oncall.llm.registry import CATALOG, estimate_cost
from ai_oncall.models import LlmCallRecord
from ai_oncall.settings import settings

logger = logging.getLogger(__name__)


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
            record = LlmCallRecord(
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
            self.records.append(record)
            _maybe_export_langfuse(record, prompt_hash=prompt_hash)


def _maybe_export_langfuse(record: LlmCallRecord, *, prompt_hash: str) -> None:
    """Fire-and-forget Langfuse export. No-op when keys are unset.

    Uses Langfuse's HTTP ingestion contract (``/api/public/ingestion``) so we
    don't need the SDK. One ``generation`` event per LLM call. Failures are
    logged and swallowed — the RCA pipeline never blocks on telemetry."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return
    try:
        import httpx

        token = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode("utf-8")
        ).decode("ascii")
        body = {
            "batch": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "generation-create",
                    "timestamp": (record.started_at or datetime.now(timezone.utc)).isoformat(),
                    "body": {
                        "id": str(uuid.uuid4()),
                        "name": f"ai-oncall.{record.stage}",
                        "model": record.model_id,
                        "input": {"prompt_hash": prompt_hash, "prompt_version": record.prompt_version},
                        "usage": {
                            "input": record.tokens_in or 0,
                            "output": record.tokens_out or 0,
                            "total": (record.tokens_in or 0) + (record.tokens_out or 0),
                            "unit": "TOKENS",
                            "totalCost": record.cost_usd,
                        },
                        "metadata": {
                            "stage": record.stage,
                            "latency_ms": record.latency_ms,
                            "error": record.error,
                        },
                    },
                }
            ]
        }
        with httpx.Client(timeout=2.0) as c:
            c.post(
                f"{settings.langfuse_host.rstrip('/')}/api/public/ingestion",
                headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
                json=body,
            )
    except Exception:
        logger.warning("langfuse_export_failed", extra={"stage": record.stage}, exc_info=False)


def _resolve_model() -> tuple[str, str]:
    for alias, spec in CATALOG.items():
        if spec["id"] == settings.rca_model:
            return spec["id"], alias
    return settings.rca_model, "mock"
