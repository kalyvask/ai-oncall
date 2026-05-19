"""Pydantic-settings, .env-driven. One source of truth for runtime config.

Defaults track BRIEF.md §9 (claude-haiku, $0.50 ceiling, sqlite store in dev).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TelemetryDriver = Literal["sqlite", "duckdb", "snowflake", "live"]
LlmProvider = Literal["anthropic", "openai", "mock"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AI_ONCALL_", extra="ignore")

    telemetry_store: TelemetryDriver = "sqlite"
    llm_provider: LlmProvider = "mock"
    rca_model: str = "claude-haiku-4-5-20251001"
    cost_ceiling_usd: float = Field(default=0.50, ge=0)
    latency_budget_seconds: float = Field(default=30.0, ge=1.0)
    log_json: bool = False
    data_dir: str = "data"
    # When True, prompts containing high-confidence secrets (AWS keys,
    # bearer tokens, JWTs, private keys, etc.) raise RawSecretsBlocked
    # instead of being sent — even after redaction would have rewritten
    # the match. Trades a tighter privacy posture for occasional refusals
    # on noisy logs. Default off; flip on for security-conscious tenants.
    raw_secrets_blocked: bool = False
    # When False, the redaction pack still runs but matches are logged
    # instead of substituted. Keep True in production; flip off for local
    # debugging only.
    redact_prompts: bool = True

    # Optional LLM-trace export. When ``langfuse_public_key`` AND
    # ``langfuse_secret_key`` are both set, the tracer POSTs a minimal span
    # per LLM call to ``langfuse_host``. No-op otherwise. The integration
    # is deliberately thin (no SDK) so the dependency footprint stays small;
    # Helicone / OTel-LLM use a similar HTTP-only contract.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Live store (AI_ONCALL_TELEMETRY_STORE=live). Required when live is
    # selected; ignored otherwise.
    prometheus_url: str | None = None
    prometheus_service_label: str = "service"
    prometheus_token: str | None = None
    loki_url: str | None = None
    loki_service_label: str = "service"
    loki_token: str | None = None

    # GitHub change-correlation (item 4). When unset, correlation falls back
    # to whatever patch_excerpt is already on the local ChangeEvent.
    github_token: str | None = None
    github_repo: str | None = None
    github_api_url: str = "https://api.github.com"

    # Slack interactivity (delivery/reactions.py). Required for the
    # /webhooks/slack/action endpoint to verify inbound payloads.
    slack_signing_secret: str | None = None

    # Slack outbound transport (delivery/send.py). Required to post the RCA
    # back to a channel and to reply in threads. When unset, the post path
    # raises SlackSendError so failures are loud.
    slack_bot_token: str | None = None
    # Default channel for posting RCA reports. Override per-tenant later if
    # multiple workspaces are wired in.
    slack_default_channel: str | None = None

    # Continuous-delivery dispatch endpoint (delivery/cd_dispatch.py). When
    # unset, the one-click rollback action records a dry-run audit and stops.
    # `cd_dispatch_secret` is the HMAC secret the receiver verifies with.
    # Refusing to dispatch without a secret is intentional: an unauthenticated
    # rollback URL on the public internet is a foot-gun.
    cd_dispatch_url: str | None = None
    cd_dispatch_secret: str | None = None

    # Inbound /webhooks/alert HMAC. When set, every POST must carry an
    # ``X-Signature: hmac-sha256=<hex>`` header computed over the raw body
    # with this secret. The same scheme PagerDuty + Grafana use. When unset,
    # the webhook accepts unsigned requests but warns; never deploy unset
    # to production.
    webhook_signing_secret: str | None = None

    # Per-tenant API tokens. Map of ``tenant_id -> bearer_token``; when set,
    # every API request (except /health, /ready, /metrics, /webhooks/slack/*)
    # must carry ``Authorization: Bearer <token>`` AND the token must match
    # the tenant declared in ``X-Tenant-Id``. Empty map keeps the dev-mode
    # behavior of "header is identity". JSON-encoded in the env var:
    # AI_ONCALL_TENANT_TOKENS='{"demo": "tok_abc", "acme": "tok_xyz"}'.
    tenant_tokens: dict[str, str] = Field(default_factory=dict)


settings = Settings()


def warn_unsafe_settings() -> list[str]:
    """Return a list of human-readable warnings about the current config.

    Called by `logging_setup.configure()` and the CLI's startup banner so
    misconfigurations show up loudly rather than silently degrading.
    """
    warnings: list[str] = []
    if settings.cd_dispatch_url and not settings.cd_dispatch_secret:
        warnings.append(
            "AI_ONCALL_CD_DISPATCH_URL is set but AI_ONCALL_CD_DISPATCH_SECRET is "
            "not. Outgoing rollback requests will not carry an HMAC signature; "
            "the receiver cannot verify the sender. Configure the secret or "
            "remove the URL."
        )
    if settings.slack_signing_secret is None:
        warnings.append(
            "AI_ONCALL_SLACK_SIGNING_SECRET is unset. The Slack interaction "
            "endpoints will reject every request because verify_slack_signature "
            "refuses to allow unsigned traffic. Set the secret to enable Slack "
            "buttons and thread Q&A."
        )
    if settings.slack_default_channel and not settings.slack_bot_token:
        warnings.append(
            "AI_ONCALL_SLACK_DEFAULT_CHANNEL is set but AI_ONCALL_SLACK_BOT_TOKEN "
            "is not. The pipeline will skip posting RCAs to Slack silently — "
            "set the bot token or remove the default channel."
        )
    return warnings
