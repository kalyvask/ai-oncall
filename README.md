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
driver that reads metrics from Prometheus and logs from Loki. The service
graph is built from observed OTel spans with a 10-minute window;
`topology.yaml` is a fallback. Schemas first, evals first, no surprise
vendor lock-in.

> Status: scaffold. The LLM call is mocked until a default model is chosen
> (BRIEF.md §13). Tests: 118/118 green. Eval: 6 synthetic fault families,
> perfect replay-mode scores; CI fails on > 5 pp drop vs the previous run.

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
2. Builds the service graph from observed OTel spans in a 10-minute window;
   falls back to `topology.yaml` when no spans are seen.
3. Plans 3-5 ranked hypotheses with the LLM, then prunes any whose claimed
   root cause is unreachable from the alerting service in the topology
   graph.
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
pytest                                                # 118 tests
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
    agent/
      plan.py                       stage 3 PLAN
      causal.py                     PRUNE — drops topology-impossible hypotheses
      investigate.py                stage 4 INVESTIGATE — tool-using loop
      synthesize.py                 stage 5 SYNTHESIZE — single-shot baseline
      correlation.py                CORRELATE — last-deploy diff per hypothesis
      staging.py                    STAGE_ACTIONS — recommend / propose / auto
      observability.py              LlmTracer — per-call prompt hash, tokens, cost
      tools.py                      6 tools (max 8 calls per incident)
      run.py                        end-to-end orchestrator
      prompts/                      versioned per-stage prompt files
    delivery/
      slack.py                      Block Kit formatter (pure)
      html.py                       static HTML export, OKLCH stylesheet
    learnings/store.py              stage 7 LEARN — append + LIKE retrieval
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
    scoring.py                      4 metrics from BRIEF §7
    cases/                          one JSON per scenario
    rcaeval_loader.py               documented stub (BRIEF §11 step 9)
    openrca_loader.py               documented stub

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
| 1 RECEIVE | `ingest/alerts.py` | ✅ schema-validated, multi-tenant |
| 2 ASSEMBLE | `topology/builder.py` + `topology/from_spans.py` | ✅ live spans (10-min window) with `topology.yaml` fallback |
| 3 PLAN | `agent/plan.py` | ✅ via MockLlm |
| 3a PRUNE | `agent/causal.py` | ✅ topology-reachability pruner |
| 4 INVESTIGATE | `agent/investigate.py` | ✅ tool-using loop, 8-call cap |
| 5 SYNTHESIZE | `agent/synthesize.py` | ✅ single-shot baseline |
| 5a CORRELATE | `agent/correlation.py` | ✅ last-deploy diff per hypothesis (local + GitHub) |
| 5b STAGE_ACTIONS | `agent/staging.py` | ✅ recommend / propose / auto tiers |
| 6 POST | `delivery/{slack,html}.py` | ✅ pure formatters |
| 7 LEARN | `learnings/store.py` | ✅ append + LIKE retrieval |

## The 6 tools the LLM gets (BRIEF.md §6)

`query_metrics` · `query_logs` · `get_recent_deploys` · `get_runbook` ·
`get_topology` · `get_past_incidents`. All deterministic, all return small
structured payloads, all pinned to `tenant_id`. Hard cap: 8 calls per incident.

## Multi-tenancy

`X-Tenant-Id` header on every request; `tenant_id` column on every row;
filter enforced at the query layer. No login screen, no RBAC. Identity is
the deployment's job (BRIEF.md §8 / §12).

## Eval

```bash
make eval                                      # synthetic, 6 cases, replay mode
python -m evals.harness --track rcaeval \
  --data-dir /path/to/RCAEval                  # stub today, skips with exit 0
python -m evals.harness --track openrca \
  --data-dir /path/to/OpenRCA                  # stub today, skips with exit 0

# Drift detection: snapshot a run, then fail CI on any > 5 pp drop.
python -m evals.harness --emit-json runs/2026-05-05.json
python -m evals.harness --baseline runs/2026-05-05.json
```

The synthetic track passes `make eval` with perfect 1.00 / 1.00 / 2.00 / 1.00
in replay mode; thresholds (0.80 / 0.50 / 1.50 / 0.80) leave headroom for the
real LLM regression once a default model is chosen. The drift mode reports
per-metric per-family deltas and exits non-zero on regressions, so CI can
catch a prompt or model change that quietly degrades a single fault family.

## LLM observability

Every PLAN and SYNTHESIZE round-trip is captured as an `LlmCallRecord` on
`Investigation.llm_calls`: stage, prompt version (e.g. `plan_v1`), prompt
hash (sha256 prefix), model id, tokens in / out, cost, latency, error.
The reasoning-trace tab in the Next.js UI reads from this list. Records
are produced by `agent/observability.py:LlmTracer`; an external sink
(Langfuse / Helicone / OTel-LLM) plugs in by reading the same list and is
on the roadmap.

## Roadmap

1. Sandboxed runner for `auto` actions and a Slack approval button for
   `propose` actions. Staging classifies the tier today; the execution
   layer plugs in on top.
2. APM/traces backend for the `live` driver (Honeycomb or Datadog), so
   the dynamic topology builder works end-to-end without a separate OTel
   ingest path.
3. Optional LLM-trace export sink via `AI_ONCALL_LLM_TRACE_SINK`
   (Langfuse / Helicone / OTel-LLM). The contract on
   `Investigation.llm_calls` is already shaped to map onto any of those.
4. Embeddings-backed past-incident retrieval, replacing the SQL `LIKE`
   in `learnings/store.py`.
5. Multi-alert correlation and deduplication before the agent spends
   tokens.
6. Specialist sub-agents (K8s, AWS, metrics, code) with a parallel
   router.
7. Wire the RCAEval and OpenRCA stubs in `evals/` to a real harness.
8. Confidence tiers in the RCA output, mapped onto the existing
   ranked-hypothesis schema.
9. Post-mortem auto-draft plus Jira / Linear ticket creation for
   follow-ups.
10. Anomaly detection on SLIs and post-deploy verification.
11. MCP server, so Cursor and Claude Desktop can drive investigations.
12. Live cost meter with auto-degrade to a cheaper model at 80% of
    `AI_ONCALL_COST_CEILING_USD`.
13. PagerDuty / incident.io ingest, replacing the synthetic webhook in
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
