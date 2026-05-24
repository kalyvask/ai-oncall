# ai-oncall

**ai-oncall** is an LLM agent that diagnoses production incidents.

When the alert fires at 3am, it pulls the last 30 minutes of telemetry, the
last 24 hours of deploys, the live service topology, and any matching
runbook; runs a tool-using investigation loop (max 8 calls, 6 deterministic
tools); and posts a ranked, evidence-backed RCA to Slack inside 30 seconds.
Each hypothesis pins to specific tool-call evidence, gets correlated to the
diff of the last deploy on the implicated service, and is shipped with a
remediation action staged into `recommend` / `propose` / `auto` tiers.

Single Python process plus a Next.js dashboard. SQLite for dev, DuckDB for
single-node prod, Snowflake for multi-tenant prod, plus an opt-in `live`
driver that reads metrics from Prometheus and logs from Loki. The core
primitive is the **causal dependency graph** — a tenant-scoped, directed
service graph built from observed OTel spans (10-minute window;
`topology.yaml` is the fallback) that the PRUNE step walks to drop
structurally impossible hypotheses before the LLM spends its 8-call budget
on them. Schemas first, evals first, no surprise vendor lock-in.

> Status: durable pipeline wired end-to-end; real Anthropic adapter (Haiku 4.5
> default, structured-output + retry-on-429/5xx + per-instance cost ceiling).
> Tests: 280 green. Eval: 6 synthetic fault families, 5 starter cases from
> public postmortems (Cloudflare 2022, Datadog 2023, AWS 2021, GitHub 2018,
> Atlassian 2022), plus RCAEval RE3-OB and OpenRCA Bank loaders for
> standardized real-data benchmarks. CI fails on > 5 pp drop vs the previous
> run.

The product contract lives in [BRIEF.md](BRIEF.md). The visual contract for
the Next.js UI lives in [UI_DESIGN.md](UI_DESIGN.md). Read those before
changing anything.

## Personalize this for your own use

If you've forked or cloned this repo, do these steps in order before running
anything against your own infrastructure. See [Quick start](#quick-start)
below for full env-var details and the install commands.

1. **Copy `.env.example` to `.env`** at the repo root. Do not commit `.env`
   — it is already in `.gitignore`.
2. **Set `ANTHROPIC_API_KEY`** in `.env` to your own Anthropic key. Required
   the moment you flip `AI_ONCALL_LLM_PROVIDER` off `mock`.
3. **Pick your LLM provider and model.** Set `AI_ONCALL_LLM_PROVIDER`
   (`anthropic` / `openai` / `mock`) and `AI_ONCALL_RCA_MODEL` (default
   `claude-haiku-4-5-20251001`). Adjust `AI_ONCALL_COST_CEILING_USD` to
   your per-incident budget.
4. **Pick your telemetry store.** Set `AI_ONCALL_TELEMETRY_STORE` to
   `sqlite` (dev), `duckdb` (single-node prod), `snowflake` (multi-tenant
   prod, stubbed today), or `live` (reads metrics from Prometheus and logs
   from Loki at incident time). For `live`, set
   `AI_ONCALL_PROMETHEUS_URL`, `AI_ONCALL_LOKI_URL`, and any bearer tokens
   (`AI_ONCALL_PROMETHEUS_TOKEN`, `AI_ONCALL_LOKI_TOKEN`); the service
   label defaults to `service` and is overridable per backend.
5. **Replace `topology.yaml`** with your own services and dependency edges.
   The file is now a fallback only — when OTel spans are present the graph
   is rebuilt from observed parent/child relationships in a 10-minute
   window. The shipped yaml is a synthetic checkout / cart / payment graph
   used by the fixtures.
6. **Replace the runbooks in `runbooks/`.** The shipped `checkout.md` is a
   sample. Drop in one Markdown runbook per service or failure family the
   agent should be able to retrieve via `get_runbook`.
7. **Wire your Slack workspace.** `ai_oncall/delivery/slack.py` is a pure
   Block Kit formatter; supply your Slack bot token and target channel via
   the transport layer (`send.py`, TODO in this scaffold) when you enable
   real posting.
8. **Wire your alert sources.** Point PagerDuty / Slack / OTLP / manual
   webhooks at the FastAPI server (`POST /alerts`). Each request must
   include an `X-Tenant-Id` header — tenancy is the deployment's job, there
   is no login screen.
9. **Per-tenant config.** All rows are filtered by `tenant_id`; pick a
   tenant ID per customer or per environment and pass it on every request.
10. **Local state stays local.** SQLite/DuckDB databases land in `data/`
    and `learnings.jsonl` is appended in place. Both paths are gitignored —
    do not commit them.
11. **Fork the eval cases.** Edit `evals/cases/` and
    `fixtures/synthetic_alerts/` to reflect your own fault families before
    you trust eval scores as a regression signal.
12. **Wire GitHub change correlation (optional).** Set
    `AI_ONCALL_GITHUB_REPO` (`owner/name`) and `AI_ONCALL_GITHUB_TOKEN` to
    have the agent fetch the diff of the most recent deploy on each
    hypothesis's `root_cause_service` and attach it as evidence. When
    unset, correlation falls back to whatever `patch_excerpt` is already on
    the local `ChangeEvent`. `AI_ONCALL_GITHUB_API_URL` is overridable for
    GitHub Enterprise.

## What it does

1. Receives an alert (PagerDuty / Slack / manual / OTLP).
2. Builds the causal dependency graph from observed OTel spans in a 10-minute
   window; falls back to `topology.yaml` when no spans are seen.
3. Plans 3-5 ranked hypotheses with the LLM, then prunes any whose claimed
   root cause is unreachable from the alerting service in the causal
   dependency graph.
4. Runs up to 8 deterministic tool calls against telemetry (live Prometheus
   + Loki when `AI_ONCALL_TELEMETRY_STORE=live`), deploys, topology, and
   runbooks.
5. Synthesizes a single ranked RCA report against `schemas/rca_report.json`,
   then correlates each hypothesis to the diff of the last deploy on its
   `root_cause_service` (local store first, GitHub on miss).
6. Stages each remediation action into `recommend` (default), `propose`
   (Slack approval button), or `auto` (whitelisted runbooks only).
7. Posts to Slack as Block Kit; renders to HTML for post-mortems; serves a
   Next.js dashboard with a reasoning trace tab.
8. Captures 👍 / 👎 / "wrong root cause" reactions into `learnings.jsonl`.

## Quick start

```bash
# Backend
pip install -e ".[dev]"
pytest                                                # 280 tests
python -m evals.harness                               # synthetic eval, 6 cases
python -m evals.harness --emit-json runs/today.json   # snapshot for drift baseline
python -m evals.harness --baseline runs/today.json    # fail on > 5 pp drop

# End-to-end (fixture mode — no LLM key required)
python -m ai_oncall.cli rca \
  fixtures/synthetic_alerts/checkout_regression.json \
  --fixture-report fixtures/expected_reports/checkout_regression.json

# FastAPI server
uvicorn ai_oncall.server:app --reload --port 8000
curl -H 'X-Tenant-Id: demo' http://localhost:8000/topology

# Frontend
cd web && npm install && npm run dev                  # port 3050 via launch.json
```

## Repo layout

```
ai-oncall/
  BRIEF.md                          design contract — single source of truth
  KICKOFF.md                        pasteable prompt for fresh CC sessions
  UI_DESIGN.md                      OKLCH palette, type scale, anti-patterns

  schemas/                          JSON Schemas (alert, telemetry_record,
                                    topology_snapshot, change_event,
                                    investigation_plan, rca_report)
  ai_oncall/
    models.py                       Pydantic mirrors of every schema
    schema_loader.py                referencing-based JSON Schema validator
    settings.py                     pydantic-settings, .env-driven
    server.py                       FastAPI + tenant middleware
    cli.py                          Typer CLI: schemas, validate-fixture, rca
    ingest/alerts.py                stage 1 RECEIVE
    topology/
      builder.py                    stage 2 ASSEMBLE — live spans + yaml fallback
      from_spans.py                 pure: spans -> TopologySnapshot
      causal_graph.py               CausalGraph — first-class graph the agent reasons over
    agent/
      plan.py                       stage 3 PLAN
      causal.py                     PRUNE — drops graph-impossible hypotheses
      investigate.py                stage 4 INVESTIGATE — tool-using loop
      synthesize.py                 stage 5 SYNTHESIZE — single-shot baseline
      calibration.py                stage 5b — deterministic abstention rules
      correlation.py                CORRELATE — last-deploy diff per hypothesis
      staging.py                    STAGE_ACTIONS — recommend / propose / auto
      observability.py              LlmTracer — per-call prompt hash, tokens, cost
      tools.py                      6 tools (max 8 calls per incident)
      replay.py                     re-run pipeline on a stored incident; diff
      run.py                        end-to-end orchestrator
      prompts/                      versioned per-stage prompt files
    delivery/
      slack.py                      Block Kit formatter (pure)
      send.py                       Slack chat.postMessage transport (httpx)
      reactions.py                  Slack signature verify + action dispatcher
      cd_dispatch.py                signed HMAC POST to a CD endpoint
      thread_qa.py                  bounded follow-up investigation in a thread
      html.py                       static HTML export, OKLCH stylesheet
    learnings/
      store.py                      stage 7 LEARN — append + LIKE retrieval
      incidents.py                  full RCA persistence + typed memory graph + thread map
      feedback_loop.py              👎 reactions -> eval regression fixtures
    storage/
      base.py                       TelemetryStore interface
      sqlite.py                     dev driver
      duckdb.py                     single-node prod driver
      snowflake.py                  multi-tenant prod driver (stub, BRIEF §12)
      live.py                       opt-in: Prometheus metrics + Loki logs
      prometheus.py                 Prometheus query_range client (httpx)
      loki.py                       Loki query_range client (httpx)
      github.py                     GitHub commits API client (item 4)
      tenancy.py                    X-Tenant-Id middleware + row filter
      factory.py                    one-line driver picker

  evals/
    harness.py                      synthetic / rcaeval / openrca tracks
    scoring.py                      4 metrics from BRIEF §7 (transformer-backed cosine)
    cases/                          one JSON per scenario
    rcaeval_loader.py               RE3-OB loader: <data_dir>/<scenario>/gt.json
    openrca_loader.py               OpenRCA Bank loader: <data_dir>/incidents/*.json

  fixtures/synthetic_alerts/        6 alerts, one per fault family
  fixtures/expected_reports/        hand-authored expected RCAs
  runbooks/checkout.md              example runbook the agent retrieves
  topology.yaml                     static topology fallback

  web/                              Next.js 15 App Router + Tailwind + OKLCH
    app/page.tsx                    incidents inbox
    app/incidents/[id]/page.tsx     incident detail with reasoning trace
    app/topology/page.tsx
    app/runbooks/page.tsx
    app/settings/page.tsx
    components/                     Card, Pill, Statline, TopNav

  tests/contracts/                  schema round-trip tests
  tests/integration/                stage 1-7 + driver + agent loop tests
```

## Pipeline

The seven BRIEF.md §4 stages plus three deterministic steps that sit
around the LLM (PRUNE before INVESTIGATE; CORRELATE and STAGE_ACTIONS
after SYNTHESIZE).

| Step | Module | Status |
|---|---|---|
| 1 RECEIVE | `ingest/alerts.py` | ✅ schema-validated, multi-tenant, HMAC-signed webhook |
| 1a ENQUEUE | `ai_oncall/jobs/` | ✅ durable SQLite job queue, idempotent on `(tenant, alert_id)`, async worker, delivery outbox with retries |
| 2 ASSEMBLE | `topology/builder.py` + `topology/from_spans.py` | ✅ live spans (10-min window) with `topology.yaml` fallback |
| 3 PLAN | `agent/plan.py` | ✅ Anthropic adapter (Haiku 4.5 default) or MockLlm; PII/secret redaction before send |
| 3a PRUNE | `agent/causal.py` | ✅ topology-reachability pruner |
| 4 INVESTIGATE | `agent/investigate.py` | ✅ tool-using loop, 8-call cap |
| 5 SYNTHESIZE | `agent/synthesize.py` | ✅ single-shot baseline |
| 5a CORRELATE | `agent/correlation.py` | ✅ last-deploy diff per hypothesis (local + GitHub) |
| 5b STAGE_ACTIONS | `agent/staging.py` | ✅ recommend / propose / auto tiers |
| 5c CALIBRATE | `agent/calibration.py` | ✅ deterministic abstention (4 rules) |
| 6 POST | `delivery/{slack,html,send}.py` | ✅ pure formatters + interactive buttons + httpx Slack transport |
| 6a SLACK ACTIONS | `delivery/reactions.py` + `cd_dispatch.py` | ✅ signed handler, HMAC-signed CD dispatch |
| 6b SLACK THREAD Q&A | `delivery/thread_qa.py` | ✅ bounded follow-up loop (≤3 tool calls), reply auto-posted |
| 7 LEARN | `learnings/{store,incidents,feedback_loop}.py` | ✅ append + retrieval + RCA persistence + typed memory graph + 👎 → eval fixture |
| REPLAY | `agent/replay.py` + `ai-oncall replay <id>` | ✅ re-runs pipeline on stored incident, diffs vs original |
| PROMOTE | `ai-oncall promote <id> --tier verified` | ✅ moves an incident across `local` / `aggregated` / `verified` tiers |

## The 6 tools the LLM gets (BRIEF.md §6)

`query_metrics` · `query_logs` · `get_recent_deploys` · `get_runbook` ·
`get_topology` · `get_past_incidents`. All deterministic, all return small
structured payloads, all pinned to `tenant_id`. Hard cap: 8 calls per incident.

## Multi-tenancy

`X-Tenant-Id` header on every request; `tenant_id` column on every row;
filter enforced at the query layer. No login screen, no RBAC. Identity is
the deployment's job (BRIEF.md §8 / §12).

The `/webhooks/slack/*` endpoints are the only exemption: Slack signs each
request with its own secret (verified at the edge) and the tenant is
recovered from the persisted incident referenced in the action payload.

## Cupcake additions (the engineer-facing 60-second loop)

Five additions on top of the baseline pipeline that close Sam's 60-second
decision moment without leaving Slack:

1. **Calibrated abstention** (`agent/calibration.py`). Four deterministic
   rules — cold_start, confidence_floor, budget_exhausted, two_strong_leads —
   override the LLM's escalation flag when the evidence doesn't support a
   verdict. The top hypothesis confidence is capped to 0.40 on cold_start
   or budget_exhausted so Slack/HTML render a clear "low confidence" pill.

2. **One-click Slack rollback** (`delivery/{slack,reactions,cd_dispatch}.py`).
   `propose`-tier hypotheses render a Block Kit "Approve rollback" button.
   Clicks hit `/webhooks/slack/action`, signature-verified against
   `AI_ONCALL_SLACK_SIGNING_SECRET`, then HMAC-sign a JSON POST to
   `AI_ONCALL_CD_DISPATCH_URL`. No URL configured = audited dry-run.

3. **Slack thread Q&A** (`delivery/thread_qa.py`). A reply in the parent
   thread ("show me the p99", "any errors at the spike?") triggers a scoped
   investigation: same six tools, hard cap of 3 calls, narrowed to one
   hypothesis. Reply posted back as Block Kit. The endpoint resolves the
   parent's `report_id` from the embedded context block (no thread-ts → id
   table needed).

4. **`replay` command** (`agent/replay.py`). Re-runs the full pipeline on
   any stored incident and emits a structured diff vs. the original report:
   verdict ∈ {match, drift, regression, improvement}. Single id or batch via
   `--batch-from`; CI uses `--fail-on-regression` to catch prompt / model
   changes that quietly degrade a fault family.

5. **Typed memory graph** (`learnings/incidents.py`). Every RCA report is
   persisted to `data/incidents.sqlite` with three trust tiers (`local`,
   `aggregated`, `verified`). A second table aggregates `(tenant, service)
   → root_cause_class` counts so `get_past_incidents` returns both recent
   incidents AND a per-service prior. Replay and calibration both read from
   here.

### CLI for the new surfaces

```bash
ai-oncall replay <report_id>                       # one incident, exit 1 on regression
ai-oncall replay --batch-from runs/curated.txt     # CI form (one id per line)
ai-oncall replay <report_id> --json --no-fail-on-regression  # snapshot for diffing
```

### Slack endpoints

| Path | Purpose |
|---|---|
| `POST /webhooks/slack/action` | Block Kit interaction (button click). Signature-verified. |
| `POST /webhooks/slack/event` | Events API: thread reply → scoped Q&A. URL handshake on first setup. |

### Slack outbound transport

`delivery/send.py` posts the RCA back to Slack. Two surfaces:

- `post_rca(report, channel)` posts the parent (Block Kit) plus the
  alternatives reply in the same thread, and persists the
  `(channel, thread_ts) -> report_id` mapping so future thread events can
  attribute replies to the right report.
- `post_thread_reply(channel, thread_ts, blocks)` posts a block list as a
  threaded reply. Used by the thread Q&A endpoint after computing an answer.

When `AI_ONCALL_SLACK_BOT_TOKEN` and `AI_ONCALL_SLACK_DEFAULT_CHANNEL` are
set, `run_rca` posts the RCA automatically at the end of stage 6. Without
the bot token, the pipeline runs unchanged and the renderers stay pure.

### Negative-feedback eval loop

`learnings/feedback_loop.py` reads 👎 / wrong-root-cause reactions from
`learnings.jsonl`, looks each one up in the persisted incidents, and
emits one regression-test fixture per mistake. Pair with explicit
`expected.root_cause` once a human supplies the correction:

```bash
ai-oncall feedback-export evals/cases/feedback   # one JSON per negative reaction
ai-oncall feedback-export evals/cases/feedback --tenant-id customer_a
ai-oncall feedback-export evals/cases/feedback --overwrite  # refresh existing
```

### Trust tier promotion

The typed memory graph stores `(tenant, service, root_cause_class)` rows
under one of three trust tiers:

- `local` — default. Only this tenant's prior incidents.
- `aggregated` — opted into cross-tenant priors (must be requested
  explicitly by callers of `get_past_incidents`).
- `verified` — a human marked the RCA right; a corroborating signal for
  cross-tenant priors.

Promote a stored incident:

```bash
ai-oncall promote <report_id> --tier verified
ai-oncall promote <report_id> --tier aggregated
```

`get_past_incidents` accepts `trust_tiers=("local",)` (default) or
`("local","verified")` to widen.

### Required env vars for the Slack surfaces

```bash
AI_ONCALL_SLACK_SIGNING_SECRET=...    # required for any Slack endpoint to accept traffic
AI_ONCALL_SLACK_BOT_TOKEN=...         # xoxb-… token; required to post the RCA + replies
AI_ONCALL_SLACK_DEFAULT_CHANNEL=...   # e.g. C123…; channel post_rca writes to
AI_ONCALL_CD_DISPATCH_URL=...         # optional; without this, rollback is a dry-run
AI_ONCALL_CD_DISPATCH_SECRET=...      # required if CD_DISPATCH_URL is set (HMAC sign)
```

Startup logs a warning when:
- `CD_DISPATCH_URL` is set without `CD_DISPATCH_SECRET` (refuses to dispatch unsigned),
- `SLACK_SIGNING_SECRET` is missing (interaction endpoints reject all traffic),
- `SLACK_DEFAULT_CHANNEL` is set without `SLACK_BOT_TOKEN` (post would silently skip).

## Deploy

The default deploy target is "single VM, single process." For the Slack
surfaces to work, the FastAPI server must be reachable from Slack's
servers — a managed PaaS (Fly.io, Railway, Render) is enough for v1.

```bash
# 1. Create the .env from the template and fill in keys (see "Personalize" §).
# 2. Build the Next.js UI once for prod.
cd web && npm install && npm run build && cd ..

# 3. Run the FastAPI server (it serves the static UI bundle at /web).
uvicorn ai_oncall.server:app --host 0.0.0.0 --port 8000

# 4. Point Slack at the public URL:
#    - Slash command / interactivity request URL: https://<host>/webhooks/slack/action
#    - Events API request URL:                    https://<host>/webhooks/slack/event
#    - Subscribe to event:                         message.channels
# 5. Point your CD system at:
#    - Rollback receiver URL: settings.cd_dispatch_url
#    - Verify the X-AI-Oncall-Signature header before acting.
```

The DuckDB / SQLite stores live in `data/` next to the process. Mount a
volume there in the deploy target so state survives container restarts.
Snowflake driver kicks in via `AI_ONCALL_TELEMETRY_STORE=snowflake` for
multi-tenant prod once a customer has telemetry there.

## Eval

```bash
make eval                                      # synthetic, 6 cases, replay mode
python -m evals.harness --track rcaeval \
  --data-dir /path/to/RCAEval                  # RE3-OB: one scenario subdir per fault
python -m evals.harness --track openrca \
  --data-dir /path/to/OpenRCA                  # OpenRCA Bank: incidents/*.json

# Drift detection: snapshot a run, then fail CI on any > 5 pp drop.
python -m evals.harness --emit-json runs/2026-05-05.json
python -m evals.harness --baseline runs/2026-05-05.json
```

The synthetic track passes `make eval` with perfect 1.00 / 1.00 / 2.00 / 1.00
in replay mode; thresholds (0.80 / 0.50 / 1.50 / 0.80) leave headroom for the
real LLM regression once a default model is chosen. The drift mode reports
per-metric per-family deltas and exits non-zero on regressions, so CI can
catch a prompt or model change that quietly degrades a single fault family.

The `rcaeval` and `openrca` tracks expect the layouts documented in
`evals/rcaeval_loader.py` and `evals/openrca_loader.py`. The loader reads
the ground-truth label and engineer narrative; the harness scores the four
metrics in replay mode today (same path as synthetic). Swap `_predict` in
`evals/harness.py` for `ai_oncall.agent.run.run_rca` to score the real
agent on the same benchmarks.

`reason_cosine` ships with a bag-of-words fallback so CI never needs a model
download. Set `AI_ONCALL_EVAL_EMBED=transformers` and install the optional
extra (`pip install -e ".[eval-embeddings]"`) to switch to
`sentence-transformers/all-MiniLM-L6-v2` embeddings — same 0.5 threshold,
much better recall on paraphrases.

## LLM observability

Every PLAN and SYNTHESIZE round-trip is captured as an `LlmCallRecord` on
`Investigation.llm_calls`: stage, prompt version (e.g. `plan_v1`), prompt
hash (sha256 prefix), model id, tokens in / out, cost, latency, error.
The reasoning-trace tab in the Next.js UI reads from this list. Records
are produced by `agent/observability.py:LlmTracer`; an external sink
(Langfuse / Helicone / OTel-LLM) plugs in by reading the same list and is
on the roadmap.

## Roadmap

Recently landed (see commit history for details): durable alert→RCA→Slack
job pipeline with idempotency and retries; real Anthropic adapter with
JSON-mode and cost ceiling; action allowlist + dry-run preview for propose
tier; `/metrics`, `/ready`, `/sloreport`, `/incidents/{id}/diff/{other}`;
HMAC-signed inbound webhooks; per-tenant bearer-token auth; PII/secret
redaction before LLM calls; optional Langfuse export; Dockerfile +
docker-compose stack.

1. Sandboxed runner for the `auto` tier (the allowlist + dry-run preview
   landed; the sandboxed execution layer is the next step).
2. APM/traces backend for the `live` driver (Honeycomb or Datadog), so
   the dynamic topology builder works end-to-end without a separate OTel
   ingest path.
3. Embeddings-backed past-incident retrieval, replacing the SQL `LIKE`
   in `learnings/store.py`.
4. Multi-alert correlation and deduplication before the agent spends
   tokens.
5. Specialist sub-agents (K8s, AWS, metrics, code) with a parallel
   router.
6. Swap `evals/harness.py:_predict` for `agent.run.run_rca` on the
   `rcaeval` / `openrca` tracks so the benchmarks score the real agent
   instead of running in replay mode.
7. Confidence tiers in the RCA output, mapped onto the existing
   ranked-hypothesis schema.
8. Post-mortem auto-draft plus Jira / Linear ticket creation for
   follow-ups.
9. Anomaly detection on SLIs and post-deploy verification.
10. MCP server, so Cursor and Claude Desktop can drive investigations.
11. Auto-degrade to a cheaper model at 80% of `AI_ONCALL_COST_CEILING_USD`
    (the cost ceiling itself is enforced today via `LlmBudgetExceeded`).
12. PagerDuty / incident.io ingest, replacing the synthetic webhook in
    `ingest/alerts.py`.

## Open decisions (BRIEF.md §13 — ask before deciding)

1. **Final product name.** `ai-oncall` is a placeholder.
2. **Default model + cost ceiling.** `claude-haiku-4-5-20251001` and `$0.50`
   per RCA today. Configurable via `AI_ONCALL_RCA_MODEL` /
   `AI_ONCALL_COST_CEILING_USD`.
3. **GitHub repo name** if this one is meant as a placeholder.
4. **Auto-action whitelist scope.** `agent/staging.py` whitelists only
   `rollback` for the `auto` tier today, gated by confidence ≥ 0.85 and
   page severity. Adding `restart`, `scale`, or `feature_flag` to the
   whitelist needs eval coverage of the false-positive rate first.

## License

MIT, see [LICENSE](LICENSE).
