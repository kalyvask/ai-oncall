# ai-oncall

**ai-oncall** is an LLM agent that diagnoses production incidents.

When the alert fires at 3am, it pulls the last 30 minutes of telemetry, the
last 24 hours of deploys, the live service topology, and any matching
runbook; runs a tool-using investigation loop (max 8 calls, 6 deterministic
tools); and posts a ranked, evidence-backed RCA to Slack inside 30 seconds.
Each hypothesis pins to specific tool-call evidence and recommends one
concrete action, prepared for one-click human approval.

Single Python process plus a Next.js dashboard. SQLite for dev, DuckDB for
single-node prod, Snowflake for multi-tenant prod. Schemas first, evals
first, no surprise vendor lock-in.

> Status: scaffold. All 10 BRIEF.md steps landed; the LLM call is mocked
> until a default model is chosen (BRIEF.md §13). Tests: 37/37 green. Eval:
> 6 synthetic fault families, perfect replay-mode scores.

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
   `sqlite` (dev), `duckdb` (single-node prod), or `snowflake` (multi-tenant
   prod). For Snowflake, supply your own connection credentials in `.env`.
5. **Replace `topology.yaml`** with your own services and dependency edges.
   The shipped file is a synthetic checkout / cart / payment graph used by
   the fixtures — your services will not match it.
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

## What it does

1. Receives an alert (PagerDuty / Slack / manual / OTLP).
2. Plans 3-5 ranked hypotheses with the LLM.
3. Runs up to 8 deterministic tool calls against telemetry, deploys,
   topology, and runbooks.
4. Synthesizes a single ranked RCA report against `schemas/rca_report.json`.
5. Posts to Slack as Block Kit; renders to HTML for post-mortems; serves a
   Next.js dashboard with a reasoning trace tab.
6. Captures 👍 / 👎 / "wrong root cause" reactions into `learnings.jsonl`.

## Quick start

```bash
# Backend
pip install -e ".[dev]"
pytest                                                # 37 tests
python -m evals.harness                               # synthetic eval, 6 cases

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
    topology/builder.py             stage 2 ASSEMBLE (static-yaml fallback)
    agent/
      plan.py                       stage 3 PLAN
      investigate.py                stage 4 INVESTIGATE — tool-using loop
      synthesize.py                 stage 5 SYNTHESIZE — single-shot baseline
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

## The seven stages (BRIEF.md §4)

| Stage | Module | Status |
|---|---|---|
| 1 RECEIVE | `ingest/alerts.py` | ✅ schema-validated, multi-tenant |
| 2 ASSEMBLE | `topology/builder.py` | ✅ static-yaml; live-spans deferred |
| 3 PLAN | `agent/plan.py` | ✅ via MockLlm |
| 4 INVESTIGATE | `agent/investigate.py` | ✅ tool-using loop, 8-call cap |
| 5 SYNTHESIZE | `agent/synthesize.py` | ✅ single-shot baseline |
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
```

The synthetic track passes `make eval` with perfect 1.00 / 1.00 / 2.00 / 1.00
in replay mode; thresholds (0.80 / 0.50 / 1.50 / 0.80) leave headroom for the
real LLM regression once a default model is chosen.

## Where this is going

The 2026 AI-SRE category is being defined by **Resolve** (multi-agent
plus graduated-trust auto-remediation), **Traversal** (causal search
engine over a continuously-learned production world model), and on the
open-source side **IncidentFox** and **OpenSRE** (breadth of
integrations, 25 to 60-plus connectors). Compared to that field, the
four moats this scaffold needs to close are:

1. Causal, graph-aware investigation rather than an LLM loop over flat tools.
2. Graduated-trust auto-remediation, not recommendations only.
3. Code-and-change correlation, tying a hypothesis to the diff that caused it.
4. Integration breadth, replacing the 6 mock tools with live observability.

The roadmap below is ordered by what closes the moat gap first, not by
implementation difficulty.

### Tier 1: close the moat gap

1. **Live observability connectors via MCP.** Prometheus, Loki or
   CloudWatch, and one APM (Datadog or Honeycomb) at minimum. Retires
   the 6 mock tools. Without this nothing else can be evaluated against
   real data.
2. **Dynamic topology from traces.** Build the service graph from a 24h
   OTel-spans window. `topology.yaml` becomes a fallback, not the source
   of truth. Foundation for everything causal.
3. **Causal hypothesis elimination over the graph.** Before the LLM
   loop, prune hypotheses that violate topology and timing constraints.
   Deterministic constraint propagation, not LLM-driven. This is
   Traversal's actual moat translated into a small open-source piece.
4. **Code-and-change correlation.** GitHub MCP connector. For each
   ranked hypothesis, fetch the diff of the last deploy on the
   implicated service and surface suspect lines as evidence.
5. **Action staging with graduated trust.** Three tiers: `recommend`
   (default), `propose` (Slack approval button), `auto` (whitelisted
   runbooks only, e.g. rollback last deploy on service X). Sandboxed
   execution. Replaces the placeholder `recommended_action` field with
   a structured surface.

### Tier 2: credibility and learning

6. Embeddings-backed past-incident retrieval, replacing the SQL `LIKE`
   in `learnings/store.py`. Hierarchical (RAPTOR-style) retrieval over
   runbooks plus post-mortems.
7. Multi-alert correlation and deduplication before the agent spends
   tokens. Single-alert mode wastes budget on storms.
8. Specialist sub-agents (K8s, AWS, metrics, code) with a router that
   dispatches in parallel. Mirrors PagerDuty GenAI and Resolve.
9. Wire the RCAEval and OpenRCA stubs in `evals/` to a real harness.
   The synthetic 6-family eval is not a regression signal anyone outside
   this repo will trust.
10. Confidence tiers in the RCA output (Bullseye / Directional /
    Probable). Maps cleanly onto the existing ranked-hypothesis
    schema.

### Tier 3: workflow

11. Post-mortem auto-draft and Jira/Linear ticket creation for follow-ups.
12. Proactive layer: anomaly detection on SLIs and post-deploy
    verification. Turns ai-oncall from reactive to proactive.
13. MCP server, so Cursor and Claude Desktop can drive investigations
    directly.
14. Live cost meter with auto-degrade to a cheaper model at 80% of the
    per-incident budget. Operationalizes the existing
    `AI_ONCALL_COST_CEILING_USD`.
15. PagerDuty / incident.io ingest, replacing the synthetic webhook in
    `ingest/alerts.py`.

If only three of these ship, do 2, 3, and 5: dynamic topology, causal
pruning, and graduated-trust actions. Those are what move this from
"LLM loop over mocked tools" to the thing Traversal and Resolve
actually sell. Integration breadth (#1) is necessary but commodity
work; the moat is in 2 through 5.

## Open decisions (BRIEF.md §13 — ask before deciding)

1. **Final product name.** `ai-oncall` is a placeholder.
2. **Default model + cost ceiling.** `claude-haiku-4-5-20251001` and `$0.50`
   per RCA today. Configurable via `AI_ONCALL_RCA_MODEL` /
   `AI_ONCALL_COST_CEILING_USD`.
3. **GitHub repo name** if this one is meant as a placeholder.
4. **Action staging** — see `BRIEF.md` §6 / `UI_DESIGN.md` §6 plus the
   `recommended_action` field on every hypothesis. Tier 1 item 5 above
   is the structured implementation; held until decisions 1-2 are made
   so we migrate the schema once.

## License

MIT, see [LICENSE](LICENSE).
