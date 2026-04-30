"""Single LLM adapter. The agent loop and the eval harness both call through
this module; nothing else imports `anthropic` directly. MockLlm replays
deterministic fixtures and is the default in tests.
"""

from __future__ import annotations

from typing import Any, Protocol


class LlmClient(Protocol):
    def generate(self, prompt: str, *, max_tokens: int = 1024, **kwargs: Any) -> dict[str, Any]:
        ...


class MockLlm:
    """Deterministic stub. Returns a canned response keyed by `prompt` prefix.

    Used by every test and by `make eval` so CI does not depend on network or keys.
    """

    def __init__(self, fixtures: dict[str, dict[str, Any]] | None = None) -> None:
        self.fixtures = fixtures or {}

    def generate(self, prompt: str, *, max_tokens: int = 1024, **kwargs: Any) -> dict[str, Any]:
        for key, response in self.fixtures.items():
            if prompt.startswith(key):
                return response
        return {"text": "", "tokens_in": len(prompt) // 4, "tokens_out": 0}


def get_client() -> LlmClient:
    """Return the LLM client configured by AI_ONCALL_LLM_PROVIDER. Real Anthropic
    wiring lands in BRIEF.md step 5; today we always return the mock."""
    return MockLlm()
