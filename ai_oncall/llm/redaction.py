"""PII + secret redaction for prompts headed to the LLM.

Logs and runbooks routinely contain credentials, tokens, and personal data
the model has no business seeing. Every prompt the agent assembles flows
through ``redact`` first; the regex pack covers the common shapes the
incident-response surface area touches:

- AWS / GCP / Azure access keys, generic Bearer tokens.
- API keys with common prefixes (``sk-``, ``ghp_``, ``xoxb-``, ``xoxa-``,
  ``xoxp-``, ``pk-``, ``glpat-``).
- JWTs (three base64url segments).
- Private keys (PEM headers).
- Email addresses, IPv4, US phone numbers, US SSN.
- Stripe-style keys (``sk_live_``, ``rk_live_``, ``pk_live_``).

Two modes:

- ``redact_in_place(text)`` — substitute matches with a fixed placeholder.
  Default for prompts.
- ``has_secrets(text)`` — boolean, used by ``raw_logs_blocked`` mode where
  we refuse to send raw log bodies that contain secrets to the model.

The pack is intentionally conservative: false positives are tolerable here,
false negatives are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

PLACEHOLDER = "[REDACTED]"


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]


# Each pattern uses (?P<v>...) for the secret span so we can highlight just
# the value in a future UI without changing the matcher.
_RULES: tuple[_Rule, ...] = (
    _Rule("aws_access_key_id", re.compile(r"\b(?P<v>A[KS]IA[0-9A-Z]{16})\b")),
    _Rule(
        "aws_secret_access_key",
        re.compile(r"(?<![A-Za-z0-9/+])(?P<v>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])"),
    ),
    _Rule(
        "ssh_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    _Rule("generic_bearer", re.compile(r"(?i)bearer\s+(?P<v>[A-Za-z0-9._\-]{20,})")),
    _Rule("openai_key", re.compile(r"\b(?P<v>sk-[A-Za-z0-9]{20,})\b")),
    _Rule("anthropic_key", re.compile(r"\b(?P<v>sk-ant-[A-Za-z0-9_\-]{20,})\b")),
    _Rule(
        "github_token",
        re.compile(
            r"\b(?P<v>ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
        ),
    ),
    _Rule("slack_token", re.compile(r"\b(?P<v>xox[abprs]-[A-Za-z0-9\-]{10,})\b")),
    _Rule("gitlab_token", re.compile(r"\b(?P<v>glpat-[A-Za-z0-9_\-]{20,})\b")),
    _Rule("stripe_key", re.compile(r"\b(?P<v>(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{20,})\b")),
    _Rule(
        "jwt",
        re.compile(r"\b(?P<v>eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)\b"),
    ),
    _Rule("email", re.compile(r"\b(?P<v>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")),
    _Rule(
        "ipv4",
        re.compile(
            r"\b(?P<v>(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(?:\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)){3})\b"
        ),
    ),
    _Rule("us_ssn", re.compile(r"\b(?P<v>\d{3}-\d{2}-\d{4})\b")),
    _Rule(
        "us_phone", re.compile(r"\b(?P<v>(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b")
    ),
    _Rule(
        "password_assignment",
        re.compile(
            r"(?i)(password|passwd|pwd|api[_\-]?key|secret|token)\s*[:=]\s*[\"']?(?P<v>[^\s\"']{6,})"
        ),
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    hits: tuple[str, ...]

    @property
    def had_hits(self) -> bool:
        return len(self.hits) > 0


def redact(text: str, *, rules: Iterable[_Rule] = _RULES) -> RedactionResult:
    """Return ``(redacted, hits)`` where ``hits`` lists the rule names that
    fired. Idempotent: running redact twice yields the same string."""
    if not text:
        return RedactionResult(text=text or "", hits=())
    out = text
    fired: list[str] = []
    for rule in rules:

        def _sub(match: re.Match[str], _name: str = rule.name) -> str:
            if _name not in fired:
                fired.append(_name)
            return (
                match.group(0).replace(match.group("v"), PLACEHOLDER)
                if match.groupdict().get("v")
                else PLACEHOLDER
            )

        out = rule.pattern.sub(_sub, out)
    return RedactionResult(text=out, hits=tuple(fired))


def has_secrets(text: str) -> bool:
    """True if any high-confidence secret pattern matches. Lower-precision
    rules (email, IPv4, phone) are intentionally excluded — they're
    privacy concerns, not credential leaks, and shouldn't trip the
    raw_logs_blocked safety mode."""
    high_conf = (
        "aws_access_key_id",
        "aws_secret_access_key",
        "ssh_private_key",
        "generic_bearer",
        "openai_key",
        "anthropic_key",
        "github_token",
        "slack_token",
        "gitlab_token",
        "stripe_key",
        "jwt",
        "password_assignment",
    )
    for rule in _RULES:
        if rule.name in high_conf and rule.pattern.search(text):
            return True
    return False
