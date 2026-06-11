# Operating ai-oncall

Everything you need to run ai-oncall against your own infrastructure:
configuration, Slack setup, deploy, the audit chain, trust tiers, the
feedback loop, and the eval tracks. The product contract is in
[BRIEF.md](BRIEF.md); the UI contract is in [UI_DESIGN.md](UI_DESIGN.md).

## Setup checklist

Do these steps in order before running anything against your own
infrastructure.

1. **Copy `.env.example` to `.env`** at the repo root. Do not commit `.env`;
   it is already in `.gitignore`.
2. **Set `ANTHROPIC_API_KEY`** in `.env`. Required the moment you flip
   `AI_ONCALL_LLM_PROVIDER` off `mock`.
3. **Pick your LLM provider and model.** Set `AI_ONCALL_LLM_PROVIDER`
   (`anthropic` / `openai` / `mock`) and `AI_ONCALL_RCA_MODEL` (default
   `claude-haiku-4-5-20251001`). Adjust `AI_ONCALL_COST_CEILING_USD` to
   your per-incident budget.
4. **Pick your telemetry store.** Set `AI_ONCALL_TELEMETRY_STORE` to
   `sqlite` (dev), `duckdb` (single-node prod), `snowflake` (multi-tenant
   prod, stubbed today), or `live` (reads metrics from Prometheus and logs
   from Loki at incident time). For `live`, set `AI_ONCALL_PROMETHEUS_URL`,
   `AI_ONCALL_LOKI_URL`, and any bearer tokens (`AI_ONCALL_PROMETHEUS_TOKEN`,
   `AI_ONCALL_LOKI_TOKEN`); the service label defaults to `service` and is
   overridable per backend.
5. **Replace `topology.yaml`** with your own services and dependency edges.
   The file is a fallback only: when OTel spans are present the graph is
   rebuilt from observed parent/child relationships in a 10-minute window.
   The shipped yaml is a synthetic checkout / cart / payment graph used by
   the fixtures.
6. **Replace the runbooks in `runbooks/`.** The shipped `checkout.md` is a
   sample. Drop in one Markdown runbook per service or failure family the
   agent should be able to retrieve via `get_runbook`.
7. **Wire your Slack workspace.** See [Slack surfaces](#slack-surfaces)
   below and the app manifest at `deploy/slack-app-manifest.yaml`.
8. **Wire your alert sources.** Point PagerDuty / Grafana / Alertmanager /
   manual webhooks at `POST /webhooks/alert`. See
   [integrations.md](integrations.md) for copy-paste configs. Each request
   must include an `X-Tenant-Id` header; tenancy is the deployment's job,
   there is no login screen.
9. **Set `AI_ONCALL_WEBHOOK_SIGNING_SECRET`.** When set, every alert POST
   must carry `X-Signature: hmac-sha256=<hex>` over the raw body. Never
   deploy to production without it; startup logs a warning when unset.
10. **Per-tenant config.** All rows are filtered by `tenant_id`; pick a
    tenant ID per customer or per environment and pass it on every request.
    Set `AI_ONCALL_TENANT_TOKENS` (JSON map of tenant to bearer token) to
    require `Authorization: Bearer` per tenant.
11. **Local state stays local.** SQLite/DuckDB databases land in `data/`
    and `learnings.jsonl` is appended in place. Both paths are gitignored.
12. **Fork the eval cases.** Edit `evals/cases/` and
    `fixtures/synthetic_alerts/` to reflect your own fault families before
    you trust eval scores as a regression signal.
13. **Wire GitHub change correlation (optional).** Set
    `AI_ONCALL_GITHUB_REPO` (`owner/name`) and `AI_ONCALL_GITHUB_TOKEN` to
    have the agent fetch the diff of the most recent deploy on each
    hypothesis's `root_cause_service` and attach it as evidence.
    `AI_ONCALL_GITHUB_API_URL` is overridable for GitHub Enterprise.

## Deploy

The default deploy target is a single VM, single process. For the Slack
surfaces to work, the FastAPI server must be reachable from Slack's
servers; a managed PaaS (Fly.io, Railway, Render) is enough for v1.

```bash
# Local stack with Prometheus + Loki for the `live` driver:
docker compose up --build          # API :8000, Prometheus :9090, Loki :3100

# Or build and run just the API image:
docker build -t ai-oncall .
docker run --env-file .env -p 8000:8000 -v ai_oncall_data:/app/data ai-oncall
```

Without Docker:

```bash
cd web && npm install && npm run build && cd ..
uvicorn ai_oncall.server:app --host 0.0.0.0 --port 8000
```

The DuckDB / SQLite stores live in `data/` next to the process. Mount a
volume there so state survives container restarts. The Snowflake driver
kicks in via `AI_ONCALL_TELEMETRY_STORE=snowflake` for multi-tenant prod
once a customer has telemetry there.

Point Slack at the public URL:

- Interactivity request URL: `https://<host>/webhooks/slack/action`
- Events API request URL: `https://<host>/webhooks/slack/event`
  (subscribe to `message.channels`)

Point your CD system's rollback receiver at `AI_ONCALL_CD_DISPATCH_URL`
and verify the `X-AI-Oncall-Signature` header before acting.

## Slack surfaces

| Path | Purpose |
|---|---|
| `POST /webhooks/slack/action` | Block Kit interaction (button click). Signature-verified. |
| `POST /webhooks/slack/event` | Events API: thread reply triggers scoped Q&A. URL handshake on first setup. |

Required env vars:

```bash
AI_ONCALL_SLACK_SIGNING_SECRET=...    # required for any Slack endpoint to accept traffic
AI_ONCALL_SLACK_BOT_TOKEN=...         # xoxb token; required to post the RCA + replies
AI_ONCALL_SLACK_DEFAULT_CHANNEL=...   # e.g. C123; channel post_rca writes to
AI_ONCALL_CD_DISPATCH_URL=...         # optional; without this, rollback is a dry-run
AI_ONCALL_CD_DISPATCH_SECRET=...      # required if CD_DISPATCH_URL is set (HMAC sign)
```

Startup logs a warning when `CD_DISPATCH_URL` is set without
`CD_DISPATCH_SECRET` (refuses to dispatch unsigned), when
`SLACK_SIGNING_SECRET` is missing (interaction endpoints reject all
traffic), when `SLACK_DEFAULT_CHANNEL` is set without `SLACK_BOT_TOKEN`
(post would silently skip), and when `WEBHOOK_SIGNING_SECRET` is unset
(the alert webhook accepts unsigned posts).

`delivery/send.py` posts the RCA back to Slack: `post_rca(report,
channel)` posts the parent (Block Kit) plus the alternatives reply in the
same thread and persists the `(channel, thread_ts) -> report_id` mapping;
`post_thread_reply` posts a block list as a threaded reply (used by thread
Q&A). When `AI_ONCALL_SLACK_BOT_TOKEN` and
`AI_ONCALL_SLACK_DEFAULT_CHANNEL` are set, `run_rca` posts the RCA
automatically at the end of stage 6.

### One-click rollback

`propose`-tier hypotheses render an "Approve rollback" button. Clicks hit
`/webhooks/slack/action`, are signature-verified against
`AI_ONCALL_SLACK_SIGNING_SECRET`, then HMAC-sign a JSON POST to
`AI_ONCALL_CD_DISPATCH_URL`. No URL configured means an audited dry-run.

### Thread Q&A

A reply in the parent thread ("show me the p99", "any errors at the
spike?") triggers a scoped investigation: same six tools, hard cap of 3
calls, narrowed to one hypothesis. The reply is posted back as Block Kit.

### Calibrated abstention

Four deterministic rules in `agent/calibration.py` (cold_start,
confidence_floor, budget_exhausted, two_strong_leads) override the LLM's
escalation flag when the evidence doesn't support a verdict. The top
hypothesis confidence is capped at 0.40 on cold_start or budget_exhausted
so Slack and HTML render a clear low-confidence pill.

## Audit chain

Every state-changing action (today: `approve_rollback` via the Slack
button; the same hook fires for future `auto`-tier dispatches) is appended
to `data/audit.jsonl` as an `AuditRecord` with five governance fields plus
a SHA-256 hash chain:

| Field | What it captures |
|---|---|
| `intent_proposal` | What the agent or approver intended (the action + rationale) |
| `contextual_state` | Top hypothesis confidence, root cause service, alert severity |
| `policy_decision` | Tier (`propose` / `auto`), approver identity, approval source |
| `execution_boundaries` | Action kind, target service, params, runbook ref |
| `actual_outcome` | Success boolean + detail (dispatched, dry-run, error) |

`record_hash = sha256(prev_hash || canonical_json(fields))`. The first row
links to a 64-zero genesis hash. `ai-oncall audit verify` re-walks the
file and reports the first index where the chain breaks, which is enough
to detect insertion, deletion, in-place edit, or reordering without
separate signing infrastructure.

```bash
ai-oncall audit list --limit 20
ai-oncall audit verify
```

## Trust tiers and the typed memory graph

Every RCA report is persisted to `data/incidents.sqlite`. A second table
aggregates `(tenant, service) -> root_cause_class` counts so
`get_past_incidents` returns both recent incidents and a per-service
prior. Rows carry one of three trust tiers:

- `local` (default): only this tenant's prior incidents.
- `aggregated`: opted into cross-tenant priors (must be requested
  explicitly by callers of `get_past_incidents`).
- `verified`: a human marked the RCA right; a corroborating signal for
  cross-tenant priors.

```bash
ai-oncall promote <report_id> --tier verified
ai-oncall promote <report_id> --tier aggregated
```

## Replay

Re-runs the full pipeline on any stored incident and emits a structured
diff vs the original report (verdict: match, drift, regression,
improvement). CI uses `--fail-on-regression` to catch prompt or model
changes that quietly degrade a fault family.

```bash
ai-oncall replay <report_id>
ai-oncall replay --batch-from runs/curated.txt
ai-oncall replay <report_id> --json --no-fail-on-regression
```

## Negative-feedback eval loop

`learnings/feedback_loop.py` reads thumbs-down / wrong-root-cause
reactions from `learnings.jsonl`, looks each one up in the persisted
incidents, and emits one regression-test fixture per mistake:

```bash
ai-oncall feedback-export evals/cases/feedback
ai-oncall feedback-export evals/cases/feedback --tenant-id customer_a
ai-oncall feedback-export evals/cases/feedback --overwrite
```

## Eval tracks

```bash
make eval                                      # synthetic, 6 cases, replay mode
python -m evals.harness --track rcaeval --data-dir /path/to/RCAEval
python -m evals.harness --track openrca --data-dir /path/to/OpenRCA

# Drift detection: snapshot a run, then fail CI on any > 5 pp drop.
python -m evals.harness --emit-json runs/2026-05-05.json
python -m evals.harness --baseline runs/2026-05-05.json

# Live model comparison (requires ANTHROPIC_API_KEY):
python -m evals.harness \
  --model-compare claude-haiku,claude-sonnet,claude-opus \
  --model-compare-output runs/model_compare.md
```

Replay mode scores predicted == expected, so it exercises the schema,
scoring, and aggregation paths for regression detection rather than
measuring model accuracy. The model-compare mode makes live calls and
measures RCA synthesis quality under perfect context; see the README for
the current results table. Swap `_predict` in `evals/harness.py` for
`ai_oncall.agent.run.run_rca` to score the full tool-using agent loop on
the same benchmarks.

`reason_cosine` ships with a bag-of-words fallback so CI never needs a
model download. Set `AI_ONCALL_EVAL_EMBED=transformers` and install the
optional extra (`pip install -e ".[eval-embeddings]"`) to switch to
`sentence-transformers/all-MiniLM-L6-v2` embeddings.

## LLM observability

Every PLAN and SYNTHESIZE round-trip is captured as an `LlmCallRecord` on
`Investigation.llm_calls`: stage, prompt version (e.g. `plan_v1`), prompt
hash (sha256 prefix), model id, tokens in / out, cost, latency, error.
The reasoning-trace tab in the web UI reads from this list. Set
`AI_ONCALL_LANGFUSE_PUBLIC_KEY` and `AI_ONCALL_LANGFUSE_SECRET_KEY` to
also POST a minimal span per LLM call to a Langfuse-compatible host
(`AI_ONCALL_LANGFUSE_HOST`).
