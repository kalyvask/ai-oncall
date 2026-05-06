"""Pydantic models mirroring schemas/. The JSON Schemas are authoritative for
wire format; these models are the in-process representation. Round-trip tests
in tests/contracts/ ensure the two stay in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolName = Literal[
    "query_metrics",
    "query_logs",
    "get_recent_deploys",
    "get_runbook",
    "get_topology",
    "get_past_incidents",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Lax(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- alert ------------------------------------------------------------------


class AlertSignal(_Lax):
    kind: Literal["metric_threshold", "log_pattern", "trace_anomaly", "synthetic_probe", "manual"]
    metric: str | None = None
    agg: Literal["p50", "p99", "p95", "sum", "rate", "avg"] | None = None
    value_ms: float | None = None
    threshold_ms: float | None = None
    window_minutes: int | None = Field(default=None, ge=1)


class Alert(_Lax):
    alert_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    fired_at: datetime
    source: Literal["pagerduty", "opsgenie", "grafana", "datadog", "manual", "slack"]
    severity: Literal["page", "warn", "info"]
    service: str = Field(min_length=1)
    signal: AlertSignal
    title: str
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    expected_focus_service: str | None = None


# --- telemetry --------------------------------------------------------------


class TelemetryRecord(_Strict):
    tenant_id: str
    kind: Literal["trace", "metric", "log"]
    service: str
    timestamp: datetime
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    name: str | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    status: Literal["ok", "error", "unset"] | None = None
    metric_value: float | None = None
    metric_unit: str | None = None
    severity: Literal["trace", "debug", "info", "warn", "error", "fatal"] | None = None
    body: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


# --- topology ---------------------------------------------------------------


class TopologyEvent(_Lax):
    timestamp: datetime
    kind: Literal["deploy", "alert", "config_change", "scale", "incident"]
    summary: str


class TopologyNode(_Strict):
    service: str
    status: Literal["ok", "warn", "error", "unknown"]
    last_events: list[TopologyEvent] = Field(default_factory=list, max_length=5)


class TopologyEdge(_Strict):
    from_: str = Field(alias="from")
    to: str
    calls_per_min: float | None = Field(default=None, ge=0)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    p99_ms: float | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TopologySnapshot(_Strict):
    tenant_id: str
    captured_at: datetime
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]


# --- change events ----------------------------------------------------------


class ChangeEvent(_Strict):
    tenant_id: str
    event_id: str
    service: str
    kind: Literal["pr_merged", "commit", "deploy", "config_change", "feature_flag"]
    timestamp: datetime
    actor: str
    title: str | None = None
    url: str | None = None
    sha: str | None = None
    patch_excerpt: str | None = Field(default=None, max_length=2048)
    files_changed: list[str] = Field(default_factory=list)


# --- investigation plan -----------------------------------------------------


class PlannedQuery(_Strict):
    tool: ToolName
    input: dict[str, Any]
    rationale: str | None = None


class PlannedHypothesis(_Strict):
    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    queries: list[PlannedQuery] = Field(min_length=1)


class InvestigationPlan(_Strict):
    tenant_id: str
    alert_id: str
    hypotheses: list[PlannedHypothesis] = Field(min_length=3, max_length=5)


# --- RCA report -------------------------------------------------------------


class ToolCallRecord(_Lax):
    tool: ToolName
    input: dict[str, Any]
    result_summary: str
    result_size: int | None = Field(default=None, ge=0)
    duration_ms: float = Field(ge=0)


class LlmCallRecord(_Lax):
    """One row per LLM round-trip. Attached to Investigation alongside the
    tool-call trace; the reasoning-trace tab in the UI reads from this."""

    stage: Literal["plan", "synthesize", "investigate", "other"]
    prompt_version: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=8, max_length=64)
    model_id: str
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float = Field(ge=0)
    error: str | None = None
    started_at: datetime | None = None


class Investigation(_Lax):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    llm_calls: list[LlmCallRecord] = Field(default_factory=list)


class EvidenceItem(_Lax):
    claim: str
    source: str


ActionKind = Literal["rollback", "restart", "scale", "feature_flag", "manual", "noop"]
ActionTier = Literal["recommend", "propose", "auto"]


class StagedAction(_Lax):
    """Structured form of `recommended_action` plus a trust tier.

    `recommend` (default): the agent surfaces the action; a human runs it.
    `propose`            : the agent attaches an approval button; a human
                           must click before anything runs.
    `auto`               : the action matches a whitelist (kind + confidence
                           thresholds); the runtime executes it in a sandbox.
    """

    kind: ActionKind
    service: str
    params: dict[str, Any] = Field(default_factory=dict)
    tier: ActionTier
    rationale: str | None = None
    runbook_ref: str | None = None


class Hypothesis(_Strict):
    root_cause_service: str
    root_cause_datetime: datetime | None = None
    confidence: float = Field(ge=0, le=1)
    reasoning: str
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=8)
    recommended_action: str
    runbook_link: str | None = None
    staged_action: StagedAction | None = None


class ModelRef(_Lax):
    provider: Literal["anthropic", "openai", "gemini", "mock"]
    id: str


class Escalation(_Lax):
    should_escalate: bool
    reason: str | None = None


class RcaReport(_Lax):
    report_id: str
    tenant_id: str
    alert: Alert
    generated_at: datetime
    model: ModelRef
    investigation: Investigation | None = None
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)
    escalation: Escalation | None = None
