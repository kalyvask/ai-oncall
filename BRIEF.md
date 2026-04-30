# BRIEF — `ai-oncall`

A single-process Python service that diagnoses production incidents using an
LLM. Read this file end-to-end before writing any code. Every section is
load-bearing.

> The product name `ai-oncall` is a placeholder. Confirm or replace before
> creating a GitHub repo.

---

## 1. Mission

When an alert fires on a service in a small distributed system, produce a
ranked, evidence-backed root-cause report and post it to Slack within ~30
seconds. Run as a single process locally or on one VM. Require no SaaS
sign-up beyond an LLM API key. Be honest about limits — the agent ranks
hypotheses for a human; it does not auto-remediate.

---

## 2. User, moment, win condition

### Persona — Sam

Full-stack engineer at a 12-person AI startup. On-call rotation of one. No
dedicated SRE. Friday 11pm: PagerDuty fires for `checkout` p99 > 2s. Sam has
4 minutes before the customer Slack channel notices. Sam does not want to
open Datadog at 11pm.

### Job-to-be-done

> When an alert fires, help me decide in under 60 seconds whether to:
> (a) roll back, (b) escalate to the engineer who shipped it, or (c) ignore
> as flaky.

### Win condition

Sam takes the suggested action 70% of the time without opening Datadog.

This pins every architectural decision to a concrete user moment. If a
feature does not measurably help Sam in those 60 seconds, it goes on the
roadmap, not in v1.

---

## 3. The seven inputs the agent reasons over

| # | Input | Wire format | Cardinality | Freshness budget |
|---|-------|-------------|-------------|-------------------|
| 1 | Alert envelope | JSON via `POST /webhooks/alert` or Slack `/rca` | 1 per incident | now |
| 2 | OTLP traces | protobuf via `POST /v1/traces` | ~10² spans/sec | last 30 min |
| 3 | OTLP metrics | protobuf via `POST /v1/metrics`, RED + saturation | 1-min granularity | last 30 min |
| 4 | OTLP logs | protobuf via `POST /v1/logs`, severity ≥ WARN, with trace_id | last 30 min | last 30 min |
| 5 | GitHub deploys | webhook + REST backfill, patch ≤ 2KB | last 24 h | last 24 h |
| 6 | Service topology | observed spans + 10-min decay; static `topology.yaml` fallback | live | continuous |
| 7 | Runbooks | markdown in `runbooks/`, keyed by service or alert name | static | reload on SIGHUP |

Don't let the agent improvise these. The wire formats are fixed by upstream
ecosystems (OTLP, GitHub, Slack); pin them and write contract tests.

---

## 4. Pipeline (seven stages)

Every stage has a typed input, a typed output, and a stub-replaceable
implementation. Stages 3 and 4 are the redesign and the differentiator.

```
1. RECEIVE       Normalize the alert envelope.
2. ASSEMBLE      Concurrent fetch of last-30-min telemetry, last-24-h GitHub
                 diffs, topology snapshot, matching runbooks.
3. PLAN  ★       LLM proposes 3–5 ranked hypotheses + the queries it wants
                 to run for each. Returns an InvestigationPlan.
4. INVESTIGATE ★ Tool-using loop. The agent issues PromQL/LogQL/SQL queries,
                 reads small bounded results, refines its hypothesis list.
                 Hard cap: 8 tool calls per incident.
5. SYNTHESIZE    Final ranked-hypothesis report. Each hypothesis has a
                 confidence, evidence bullets pinned to specific tool
                 results, and one remediation action.
6. POST          Slack thread: top hypothesis as parent, alternatives as
                 replies, 👍/👎/"wrong root cause" reactions captured.
7. LEARN         Reactions + manual corrections appended to learnings.jsonl.
                 Retrieved as few-shot for similar incidents on the next run.
```

★ marks the redesigned stages — the original prototype dumped everything
into a single prompt; this version reasons iteratively. That's the headline
technical bet.

---

## 5. Schemas — write these first

Pydantic + JSON-schema, both. Round-trip tests for every fixture before any
agent code is written.

- `schemas/alert.json` — incoming alert envelope.
- `schemas/telemetry_record.json` — normalized OTLP record.
- `schemas/topology_snapshot.json` — graph nodes, edges, last-N-events per node.
- `schemas/change_event.json` — GitHub PR/commit/diff.
- `schemas/investigation_plan.json` — `{hypotheses: [{statement, confidence, queries: [...]}]}`.
- `schemas/rca_report.json` — see attached file in this brief.

The schema set is the contract. If a stage's input or output cannot be
expressed in these types, the design is wrong; come back to this section
before adding fields ad hoc.

---

## 6. The six tools the LLM gets

A discrete, named set. The LLM never receives raw 100k-row logs; it gets
small structured results. Each tool has a strict input schema, a max
result size, and is deterministic given the same inputs.

```python
query_metrics(service: str, metric: str, since: ISO8601, agg: Literal["p50","p99","sum","rate"]) -> List[MetricPoint]   # ≤ 60 points
query_logs(service: str, since: ISO8601, regex: str, limit: int = 50) -> List[LogLine]                                  # ≤ 50 lines
get_recent_deploys(service: str, since: ISO8601) -> List[ChangeEvent]                                                   # ≤ 25 events
get_runbook(service: str) -> Optional[str]                                                                              # markdown
get_topology(service: str, depth: int = 2) -> TopologySubgraph                                                          # BFS subgraph
get_past_incidents(service: str, k: int = 3) -> List[PastIncident]                                                      # k-NN over learnings.jsonl
```

The LLM is the only stochastic component. Tools are pure.

---

## 7. Eval harness — design before the agent

Build the eval first, the agent against the eval. Three tracks. Four
metrics. `make eval` runs in < 3 min with `MockLlm`, < 5 min with
`claude-haiku`. CI fails on any track regression > 5% absolute.

### Tracks

- **Synthetic** — 30 hand-crafted scenarios across 6 fault families:
  deploy regression, dependency saturation, config drift, downstream
  cascade, noisy neighbor, slow leak. Easy / medium / hard.
- **RCAEval RE3-OB** — full loader, real telemetry + source.
- **OpenRCA Bank** — real production incidents. Document the
  static-topology limitation honestly.

### Metrics

| Metric | How |
|--------|-----|
| Component match | Exact case-insensitive equality vs. expected |
| Reason cosine | sentence-transformers `all-MiniLM-L6-v2`, threshold 0.5 |
| Trajectory score | LLM-as-judge against a reference tool-call sequence; rubric ∈ {0,1,2} |
| Escalation precision | When agent says "low confidence, escalate," is it actually a hard case? |

Make these visible in CI on every PR.

---

## 8. Delivery surfaces

Four. Ranked by importance at the moment of the incident.

1. **Slack** — Block Kit messages, reaction-driven feedback, threaded
   alternatives. The only surface that matters at 11pm.
2. **Web UI** — Next.js (App Router, TypeScript, Tailwind) at `/web`.
   Pages: incident inbox, single-incident detail with reasoning trace,
   topology view, runbook editor, learnings/feedback browser, settings.
   Reads from the same FastAPI backend; no separate auth layer (see
   multi-tenancy below).
3. **CLI** — `ai-oncall rca <service>` for testing and engineers who like
   the terminal.
4. **HTML report** — static export from `rca_report.json` for blameless
   post-mortems and external sharing.

### Multi-tenancy without auth

Every API request carries `X-Tenant-Id` (or `?tenant=…` for browser links).
Every DB row carries `tenant_id`. The store enforces row-level filtering at
the query layer. No login screen, no RBAC; deployment trust is delegated to
a reverse proxy or TOFU. This pattern keeps the v1 tool deployable as a
single process while supporting demo + prod + per-customer isolation later.

---

## 9. Non-functional constraints

State these explicitly so the agent doesn't over-engineer.

- **Single backend process.** FastAPI + uvicorn. The Next.js UI is a
  second process in dev, statically exported and served from FastAPI in
  prod (or proxied behind one host).
- **Pluggable telemetry store.** `TelemetryStore` interface with three
  drivers: `sqlite` (default for dev), `duckdb` (default for single-node
  prod), `snowflake` (opt-in for multi-tenant prod with shared warehouse).
  Pick the driver via `TELEMETRY_STORE=…` in `.env`. Same query surface
  across drivers; SQL dialects abstracted via SQLAlchemy core.
- **All state on disk by default.** `data/` holds `app.sqlite`,
  `telemetry.duckdb`, `changes.jsonl`, `learnings.jsonl`, `topology.yaml`.
  One `git pull && pip install` rebuilds. Snowflake driver replaces only
  the telemetry store; the other files stay local.
- **Structured logging from line one.** Standard `logging` module, JSON
  option, no `print` calls anywhere.
- **Token budget enforced.** Every prompt counts tokens (Anthropic SDK
  exposes a counter); truncates oldest evidence first; never silently
  overflows.
- **Cost ceiling per run.** Hard cap of $0.50 per RCA. Default model is
  `claude-haiku`; `claude-opus` is opt-in.
- **No `--no-verify` ever.** Pre-commit + ruff + mypy + pytest. The hooks
  fail or the commit fails.

---

## 10. Repo skeleton

```
ai-oncall/
  README.md                        Lead with "What is this?" + 30s demo gif.
  pyproject.toml                   Installable: pip install -e .
  BRIEF.md                         This file. Lives in the repo.

  schemas/                         JSON-schema (sec. 5).
  ai_oncall/
    __init__.py
    settings.py                    Pydantic-settings, .env-driven.
    logging_setup.py               Structured logging.
    server.py                      FastAPI + endpoints.
    cli.py                         Typer CLI.
    ingest/
      otlp.py
      github.py
      alerts.py                    Webhook receiver + normalizer.
      sinks.py
    topology/
      builder.py
      decay.py                     Sliding window, separated.
      retriever.py
    agent/
      plan.py                      Stage 3.
      investigate.py               Stage 4 — tool-using loop.
      synthesize.py                Stage 5.
      tools.py                     The 6 tools.
      prompts/                     One file per stage; versioned.
    delivery/
      slack.py
      html.py
    learnings/
      store.py                     Append + retrieve.
      retrieve.py                  k-NN over embeddings.
    llm/
      registry.py                  Model IDs in one place.
      client.py                    Anthropic / OpenAI / mock.

    storage/
      base.py                      TelemetryStore interface.
      sqlite.py                    Default dev driver.
      duckdb.py                    Default single-node prod driver.
      snowflake.py                 Multi-tenant prod driver (stub in v1).
      tenancy.py                   X-Tenant-Id middleware + row filter.

  web/                             Next.js App Router, TypeScript, Tailwind.
    app/
      layout.tsx
      page.tsx                     Incidents list.
      incidents/[id]/page.tsx      Incident detail w/ reasoning trace.
      topology/page.tsx
      runbooks/page.tsx
      settings/page.tsx
    components/
    lib/api.ts                     Calls FastAPI; sets X-Tenant-Id.
    package.json
    next.config.mjs

  evals/
    cases/                         JSON fixtures, one per scenario.
    scoring.py                     4 metrics from sec. 7.
    harness.py                     Runs all tracks; emits one JSON report.
    rcaeval_loader.py
    openrca_loader.py

  runbooks/                        Markdown, indexed at startup.
  topology.yaml                    Static fallback + manual overrides.
  data/                            Gitignored. SQLite + DuckDB live here.
  fixtures/                        Tiny synthetic OTLP payloads + alerts.
  tests/
    unit/
    integration/
    contracts/                     Schema round-trip tests.
  scripts/
    bench.sh                       make eval entry point.
    fakedata.py                    Generate replay-able OTLP traffic.
  .github/workflows/
    ci.yml                         Lint + tests + eval on every PR.
```

---

## 11. Order of work — strict

Do not start step N+1 until step N's eval passes.

1. Schemas + Pydantic models in `schemas/` and `ai_oncall/`. Contract
   tests round-trip every fixture.
2. Eval harness with `MockLlm`. Five synthetic scenarios. `make eval`
   green.
3. Telemetry store: `TelemetryStore` interface + SQLite + DuckDB
   drivers. Snowflake driver is a stub that raises `NotImplementedError`
   with a clear message. Same SQL surface across drivers.
4. Stages 1–2 (RECEIVE, ASSEMBLE) end-to-end against fixtures, all
   tenant-scoped.
5. Stage 5 (SYNTHESIZE) single-shot prompt. Establish baseline metrics.
6. Stages 3–4 (PLAN + INVESTIGATE), tool-using loop. Measure delta.
7. Slack delivery + reaction-driven feedback loop.
8. Web UI scaffold (Next.js App Router). Five pages: incidents list,
   incident detail with reasoning trace, topology, runbooks, settings.
   Reads via REST against the same backend; carries `X-Tenant-Id`.
9. OpenRCA + RCAEval loaders. Real-data eval. Document gaps.
10. Snowflake driver — implement once a real customer has the data
    there. Until then, the stub stays.

---

## 12. Forbidden in v1

So you don't waste cycles or overbuild.

- **No auth, no RBAC, no SSO.** Multi-tenancy is supported via
  `X-Tenant-Id` header + row-level filter. Identity is the deployment's
  job, not the app's.
- **No DB beyond SQLite / DuckDB / Snowflake.** No Postgres, no Kafka, no
  Redis, no MongoDB. The three drivers cover dev, single-node prod, and
  multi-tenant prod.
- **No live multi-page web app frameworks beyond Next.js.** Pick Next.js
  App Router and stay there. No Remix, no SvelteKit, no Astro.
- **No copying code from external repos.** If you need OpenRCA's scoring
  protocol or RCAEval's data layout, read the upstream docs and
  reimplement.
- **No streaming-by-default**, no WebSockets in v1. The UI polls the API.
  Add WS only if eval shows it materially improves Sam's 60-second flow.

---

## 13. Ask before deciding

Stop and ask before:

- choosing the final product name (`ai-oncall` is a placeholder),
- picking the default model and the cost ceiling,
- naming the GitHub repo,
- adding any new top-level dependency beyond what's in this brief.

Everything else is your call. Bias to small commits, contract tests, and
honest commit messages.
