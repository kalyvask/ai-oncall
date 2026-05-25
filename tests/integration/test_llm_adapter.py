"""Anthropic adapter unit tests.

Network is never touched. We mock the ``anthropic.Anthropic`` client and
assert: cost accumulation, budget enforcement, retry on 429/5xx, JSON-suffix
on the system prompt, model fallback when the key is missing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_oncall.llm.client import (
    AnthropicLlm,
    LlmBudgetExceeded,
    MockLlm,
    get_client,
)


def _fake_response(text: str, tokens_in: int = 100, tokens_out: int = 50):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def test_anthropic_adapter_returns_text_and_tracks_cost(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0)
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: _fake_response('{"ok": true}', 1000, 200))
    )
    llm._client = fake_client

    result = llm.generate("hello")
    assert result["text"] == '{"ok": true}'
    assert result["tokens_in"] == 1000
    assert result["tokens_out"] == 200
    assert result["cost_usd"] > 0
    assert llm.cumulative_cost_usd == result["cost_usd"]


def test_budget_ceiling_blocks_further_calls(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=0.0001)
    llm._client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response('{"x":1}', 1_000_000, 1_000_000)
        )
    )
    llm.generate("first")  # pushes cumulative over the cap
    with pytest.raises(LlmBudgetExceeded):
        llm.generate("second")


def _mk_api_status_error(status_code: int, message: str = "x"):
    """Build an APIStatusError without hitting the SDK's strict constructor."""
    import anthropic

    err = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    Exception.__init__(err, message)
    err.status_code = status_code
    err.message = message
    return err


def test_retries_on_429_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0, max_retries=2)

    call_count = {"n": 0}

    def flaky_create(**kw):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise _mk_api_status_error(429, "rate limited")
        return _fake_response('{"ok": true}')

    llm._client = SimpleNamespace(messages=SimpleNamespace(create=flaky_create))
    with patch("ai_oncall.llm.client._sleep_backoff", lambda _a: None):
        result = llm.generate("hello")
    assert call_count["n"] == 2
    assert result["text"] == '{"ok": true}'


def test_does_not_retry_on_400(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import anthropic

    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0)

    def bad_request(**kw):
        raise _mk_api_status_error(400, "bad input")

    llm._client = SimpleNamespace(messages=SimpleNamespace(create=bad_request))
    with pytest.raises(anthropic.APIStatusError):
        llm.generate("hello")


def test_get_client_returns_mock_when_provider_is_mock(monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "mock")
    client = get_client()
    assert isinstance(client, MockLlm)


def test_get_client_falls_back_to_mock_when_key_missing(monkeypatch) -> None:
    """anthropic provider requested but no API key. Don't crash the worker —
    return MockLlm and log."""
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = get_client()
    assert isinstance(client, MockLlm)


def test_unknown_model_alias_raises() -> None:
    with pytest.raises(ValueError):
        AnthropicLlm(model_alias="claude-banana", api_key="sk-test")


def test_json_suffix_appended_to_system_prompt(monkeypatch) -> None:
    """When expect_json=True (default), the adapter appends a raw-JSON
    instruction to the system message. Critical for PLAN/SYNTHESIZE which
    json.loads the response."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0)
    seen: dict = {}

    def capture(**kw):
        seen.update(kw)
        return _fake_response('{"ok":true}')

    llm._client = SimpleNamespace(messages=SimpleNamespace(create=capture))
    llm.generate("prompt", system="You are an oncall agent.")
    assert "raw JSON" in (seen.get("system") or "")
    assert "oncall agent" in (seen.get("system") or "")
