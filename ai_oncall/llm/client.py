"""Single LLM adapter. The agent loop and the eval harness both call through
this module; nothing else imports `anthropic` directly.

The contract is small: ``generate(prompt, max_tokens=...) -> {"text", "tokens_in", "tokens_out"}``.
PLAN and SYNTHESIZE rely on the model returning a JSON object as ``text``;
both stages json.loads it and validate against the schema. The Anthropic
adapter therefore wraps each call with a JSON-shaped system suffix and
short-circuits if the response can't be parsed.

Two clients today:
- ``MockLlm`` — deterministic fixture replay. Default in tests and ``make eval``.
- ``AnthropicLlm`` — real API. Selected when ``AI_ONCALL_LLM_PROVIDER=anthropic``
  AND ``ANTHROPIC_API_KEY`` is set. Falls back to MockLlm with a warning if the
  key is missing so the worker doesn't crash silently.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Protocol

from ai_oncall.llm.redaction import has_secrets, redact
from ai_oncall.llm.registry import CATALOG, estimate_cost
from ai_oncall.settings import settings

logger = logging.getLogger(__name__)


class LlmClient(Protocol):
    def generate(self, prompt: str, *, max_tokens: int = 1024, **kwargs: Any) -> dict[str, Any]: ...


class LlmBudgetExceeded(RuntimeError):
    """Raised when a request would push cumulative cost above the ceiling."""


class RawSecretsBlocked(RuntimeError):
    """Raised when ``settings.raw_secrets_blocked`` is True and the prompt
    contains a high-confidence secret pattern (AWS key, JWT, bearer token,
    private key, etc.). The caller should sanitize the bundle and retry."""


def _scrub_prompt(prompt: str) -> tuple[str, tuple[str, ...]]:
    """Apply privacy rules to a prompt. Returns ``(text, hit_rule_names)``.

    Raises ``RawSecretsBlocked`` when configured and a high-confidence
    secret is present. Redacts in place otherwise (unless redaction is
    disabled in settings, in which case it only logs)."""
    if not prompt:
        return prompt or "", ()
    if settings.raw_secrets_blocked and has_secrets(prompt):
        raise RawSecretsBlocked(
            "prompt contains a high-confidence secret pattern and raw_secrets_blocked is enabled"
        )
    if not settings.redact_prompts:
        # Log only — used by local debug runs where the operator accepts the risk.
        from ai_oncall.llm.redaction import redact as _scan

        result = _scan(prompt)
        if result.had_hits:
            logger.warning(
                "prompt_secret_detected_but_redact_disabled", extra={"hits": list(result.hits)}
            )
        return prompt, result.hits
    result = redact(prompt)
    if result.had_hits:
        logger.info("prompt_redacted", extra={"hits": list(result.hits)})
    return result.text, result.hits


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


class AnthropicLlm:
    """Production Anthropic adapter.

    Retries 5xx and 429 with exponential backoff + jitter. Caps cumulative
    cost at ``settings.cost_ceiling_usd`` per-instance — when exceeded, raises
    ``LlmBudgetExceeded`` rather than burning more spend. The synthesize stage
    sits behind the cap so a runaway loop can't drain the budget."""

    def __init__(
        self,
        *,
        model_alias: str = "claude-haiku",
        api_key: str | None = None,
        request_timeout: float = 30.0,
        max_retries: int = 4,
        cost_ceiling_usd: float | None = None,
    ) -> None:
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK not installed. `pip install anthropic` or use MockLlm."
            ) from e
        if model_alias not in CATALOG:
            raise ValueError(f"unknown model alias: {model_alias}. Known: {list(CATALOG)}")
        self.model_alias = model_alias
        self.model_id = CATALOG[model_alias]["id"]
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set. Provide via env or use MockLlm.")
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.cost_ceiling_usd = (
            cost_ceiling_usd if cost_ceiling_usd is not None else settings.cost_ceiling_usd
        )
        self._cumulative_cost_usd = 0.0
        self._client: Any = None  # lazy anthropic.Anthropic

    @property
    def cumulative_cost_usd(self) -> float:
        return self._cumulative_cost_usd

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.request_timeout)
        return self._client

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        system: str | None = None,
        expect_json: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._cumulative_cost_usd >= self.cost_ceiling_usd:
            raise LlmBudgetExceeded(
                f"cost ceiling ${self.cost_ceiling_usd:.2f} exceeded "
                f"(cumulative ${self._cumulative_cost_usd:.4f})"
            )

        # Privacy pass: redact secrets / PII or refuse outright.
        prompt, _ = _scrub_prompt(prompt)
        if system:
            system, _ = _scrub_prompt(system)

        import anthropic

        client = self._get_client()
        # Append a tail instruction asking for raw JSON when the caller expects
        # to json.loads the response. Stage prompts already say "Return JSON";
        # this is a belt-and-suspenders system suffix.
        system_msg = system or ""
        if expect_json:
            system_msg = (
                system_msg + "\n\n" if system_msg else ""
            ) + "Return your response as a single raw JSON object. No prose, no markdown fences."

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.messages.create(
                    model=self.model_id,
                    max_tokens=max_tokens,
                    system=system_msg or None,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(
                    getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
                )
                tokens_in = getattr(resp.usage, "input_tokens", 0) or 0
                tokens_out = getattr(resp.usage, "output_tokens", 0) or 0
                cost = estimate_cost(self.model_alias, tokens_in, tokens_out)
                self._cumulative_cost_usd += cost
                return {
                    "text": text,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": cost,
                    "model_id": self.model_id,
                }
            except anthropic.APIStatusError as e:  # rate-limit or 5xx
                last_error = e
                status = getattr(e, "status_code", 0)
                if status not in (408, 409, 429, 500, 502, 503, 504):
                    raise
                if attempt >= self.max_retries:
                    raise
                _sleep_backoff(attempt)
            except anthropic.APIConnectionError as e:
                last_error = e
                if attempt >= self.max_retries:
                    raise
                _sleep_backoff(attempt)
        # unreachable; raise the last error explicitly to satisfy mypy
        raise last_error or RuntimeError("anthropic request failed without exception")


def _sleep_backoff(attempt: int) -> None:
    base = min(8.0, 0.5 * (2**attempt))
    jitter = random.uniform(0, base * 0.25)
    time.sleep(base + jitter)


def get_client() -> LlmClient:
    """Return the LLM client configured by AI_ONCALL_LLM_PROVIDER.

    - ``anthropic`` -> AnthropicLlm with the model from settings.rca_model
      (mapped through CATALOG by ID, defaulting to claude-haiku).
    - ``mock`` (default) -> empty MockLlm.
    - ``openai`` -> currently not implemented; returns mock with a warning.
    """
    provider = settings.llm_provider
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning("anthropic_provider_selected_but_key_missing — falling back to MockLlm")
            return MockLlm()
        alias = _alias_for_model_id(settings.rca_model)
        return AnthropicLlm(model_alias=alias)
    if provider == "openai":
        logger.warning("openai provider not implemented yet — using MockLlm")
        return MockLlm()
    return MockLlm()


def _alias_for_model_id(model_id: str) -> str:
    for alias, spec in CATALOG.items():
        if spec["id"] == model_id:
            return alias
    return "claude-haiku"
