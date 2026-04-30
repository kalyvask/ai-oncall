# ai-oncall

Diagnoses production incidents using an LLM. When an alert fires, posts a
ranked, evidence-backed RCA to Slack within ~30 seconds. Single Python
process plus a Next.js UI.

> Status: scaffold. All 10 BRIEF.md steps landed; the LLM call is mocked
> until a default model is chosen (BRIEF.md §13). Tests: 37/37 green. Eval:
> 6 synthetic fault families, perfect replay-mode scores.

The product contract lives in [BRIEF.md](BRIEF.md). The visual contract for
the Next.js UI lives in [UI_DESIGN.md](UI_DESIGN.md). Read those before
changing anything.

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

## Open decisions (BRIEF.md §13 — ask before deciding)

1. **Final product name.** `ai-oncall` is a placeholder.
2. **Default model + cost ceiling.** `claude-haiku-4-5-20251001` and `$0.50`
   per RCA today. Configurable via `AI_ONCALL_RCA_MODEL` /
   `AI_ONCALL_COST_CEILING_USD`.
3. **GitHub repo name** if this one is meant as a placeholder.
4. **Action staging** — see `BRIEF.md` §6 / `UI_DESIGN.md` §6 plus the
   `recommended_action` field on every hypothesis. The structured action
   surface (low / medium / high blast radius, per-class approval thresholds)
   is the natural next layer; held until decisions 1-2 are made so we
   migrate the schema once.

## Forbidden in v1 (BRIEF.md §12)

No auth / RBAC / SSO. No DB beyond SQLite / DuckDB / Snowflake. No web
framework beyond Next.js App Router. No streaming / WebSockets by default.
No copying code from external repos. No `--no-verify` ever.

## License

MIT — see [LICENSE](LICENSE).
