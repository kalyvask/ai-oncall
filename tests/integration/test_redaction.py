"""Prompt-side privacy: PII + secret redaction, raw_secrets_blocked refusal.

The regex pack is intentionally tuned to false-positives on credentials,
which is the safer mistake. Every test below is the kind of leak a real
incident-response prompt would carry: copy-pasted log lines, runbook
snippets, GitHub diffs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_oncall.llm.client import AnthropicLlm, RawSecretsBlocked
from ai_oncall.llm.redaction import PLACEHOLDER, has_secrets, redact


def test_redact_substitutes_aws_access_key() -> None:
    text = "Caller used AKIAIOSFODNN7EXAMPLE in the failing request."
    r = redact(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert PLACEHOLDER in r.text
    assert "aws_access_key_id" in r.hits


def test_redact_substitutes_anthropic_key() -> None:
    text = "AI_ONCALL_ANTHROPIC_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"
    r = redact(text)
    assert "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv" not in r.text
    assert PLACEHOLDER in r.text


def test_redact_substitutes_jwt() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.AbCDeF"
    r = redact(text)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in r.text


def test_redact_substitutes_private_key_block() -> None:
    text = (
        "got key:\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nMIIEpQIBAAKCAQEA\n-----END OPENSSH PRIVATE KEY-----\n"
        "uploaded."
    )
    r = redact(text)
    assert "BEGIN OPENSSH PRIVATE KEY" not in r.text
    assert "ssh_private_key" in r.hits


def test_redact_email_and_ip() -> None:
    text = "User alex@example.com from 10.4.5.6 saw error."
    r = redact(text)
    assert "alex@example.com" not in r.text
    assert "10.4.5.6" not in r.text


def test_redact_password_assignment() -> None:
    text = "stripe_secret: sk_test_abcdefghij_dont_share_this_value"
    r = redact(text)
    # Either the assignment rule or stripe_key rule scrubs it.
    assert "abcdefghij_dont_share_this_value" not in r.text or "sk_test_abcdefghij" not in r.text


def test_redact_is_idempotent() -> None:
    text = "key=sk-abc1234567890XYZ123 and AKIAIOSFODNN7EXAMPLE"
    once = redact(text).text
    twice = redact(once).text
    assert once == twice


def test_has_secrets_true_for_credentials() -> None:
    assert has_secrets("export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE") is True


def test_has_secrets_false_for_only_email_or_ip() -> None:
    """Lower-confidence rules (email, IP) are privacy concerns, not credential
    leaks. They shouldn't trip raw_secrets_blocked."""
    assert has_secrets("alex@example.com fired from 10.4.5.6") is False


def test_anthropic_llm_redacts_before_sending(monkeypatch) -> None:
    """The adapter must scrub secrets BEFORE the SDK call. We capture
    kwargs at the boundary to verify the credentials never reached the wire."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0)
    captured: dict = {}

    def capture(**kw):
        captured.update(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    llm._client = SimpleNamespace(messages=SimpleNamespace(create=capture))
    llm.generate("Caller's key was sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv when the alert fired.")
    sent_prompt = captured["messages"][0]["content"]
    assert "sk-ant-api03" not in sent_prompt
    assert PLACEHOLDER in sent_prompt


def test_raw_secrets_blocked_refuses_high_confidence_secret(monkeypatch) -> None:
    from ai_oncall.settings import settings

    monkeypatch.setattr(settings, "raw_secrets_blocked", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0)
    llm._client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: pytest.fail("must not call client"))
    )
    with pytest.raises(RawSecretsBlocked):
        llm.generate("Authorization: Bearer eyJabc.eyJabc.signature_part_long_enough_xyz")


def test_raw_secrets_blocked_does_not_refuse_email_only(monkeypatch) -> None:
    """An email is PII but not a high-confidence secret; the prompt should
    still be sent (with redaction), not blocked."""
    from ai_oncall.settings import settings
    from types import SimpleNamespace as NS

    monkeypatch.setattr(settings, "raw_secrets_blocked", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = AnthropicLlm(model_alias="claude-haiku", cost_ceiling_usd=10.0)
    llm._client = NS(
        messages=NS(create=lambda **kw: NS(
            content=[NS(type="text", text='{"ok": true}')],
            usage=NS(input_tokens=5, output_tokens=2),
        ))
    )
    result = llm.generate("User alex@example.com saw an error.")
    assert result["text"] == '{"ok": true}'
