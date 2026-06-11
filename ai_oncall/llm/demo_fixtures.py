"""Canned MockLlm responses for demo mode (``AI_ONCALL_DEMO=1``).

The live pipeline needs a plan and a synthesis from the LLM; in demo mode
both are replayed from the bundled checkout-regression fixture so the full
webhook -> worker -> report -> web UI path runs with no API key. The mock
keys responses on ``SYSTEM_PROMPT[:60]`` prefixes, the same convention
``tests/integration/test_agent_loop.py`` uses.

Only meaningful from a repo checkout: the payloads are read from
``fixtures/``, which is not shipped inside the package or image.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_oncall.agent.prompts import plan_v1, synthesize_v1
from ai_oncall.llm.client import MockLlm

_REPO = Path(__file__).resolve().parents[2]
_EXPECTED_REPORT = _REPO / "fixtures" / "expected_reports" / "checkout_regression.json"


def demo_client() -> MockLlm:
    """Return a MockLlm preloaded with the checkout-regression demo case."""
    if not _EXPECTED_REPORT.exists():
        raise RuntimeError(
            "demo mode needs the bundled fixtures; run from a repo checkout "
            f"(missing {_EXPECTED_REPORT})"
        )
    report: dict[str, Any] = json.loads(_EXPECTED_REPORT.read_text(encoding="utf-8"))
    alert = report["alert"]
    plan: dict[str, Any] = {
        "tenant_id": alert["tenant_id"],
        "alert_id": alert["alert_id"],
        "hypotheses": [
            {
                "statement": "payment deploy regression broke checkout's downstream calls",
                "confidence": 0.7,
                "queries": [
                    {"tool": "get_topology", "input": {"service": "checkout", "depth": 2}},
                    {
                        "tool": "get_recent_deploys",
                        "input": {"service": "payment", "since": "2026-04-24T03:14:00Z"},
                    },
                ],
            },
            {
                "statement": "checkout itself regressed",
                "confidence": 0.2,
                "queries": [
                    {
                        "tool": "get_recent_deploys",
                        "input": {"service": "checkout", "since": "2026-04-24T03:14:00Z"},
                    },
                ],
            },
            {
                "statement": "external payment provider degradation",
                "confidence": 0.1,
                "queries": [
                    {"tool": "get_runbook", "input": {"service": "payment"}},
                ],
            },
        ],
    }
    return MockLlm(
        fixtures={
            plan_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(plan),
                "tokens_in": 800,
                "tokens_out": 200,
            },
            synthesize_v1.SYSTEM_PROMPT[:60]: {
                "text": json.dumps(report),
                "tokens_in": 4000,
                "tokens_out": 600,
            },
        }
    )
