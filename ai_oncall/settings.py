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
    log_json: bool = False
    data_dir: str = "data"

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
