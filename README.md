# ai-oncall

**ai-oncall** is an LLM agent that diagnoses production incidents.

When an alert fires, it pulls the last 30 minutes of telemetry, the last
24 hours of deploys, the live service topology, and any matching runbook;
runs a tool-using investigation loop (max 8 calls, 6 deterministic tools);
and posts a ranked, evidence-backed RCA to Slack inside 30 seconds. Each
hypothesis pins to specific tool-call evidence, gets correlated to the
diff of the last deploy on the implicated service, and ships with a
remediation action staged into `recommend` / `propose` / `auto` tiers.

Single Python process plus a Next.js dashboard. SQLite for dev, DuckDB for
single-node prod, Snowflake for multi-tenant prod, plus an opt-in `live`
driver that reads metrics from Prometheus and logs from Loki. The core
primitive is the causal dependency graph: a tenant-scoped, directed
service graph built from observed OTel spans (10-minute window;
`topology.yaml` is the fallback) that the PRUNE step walks to drop
structurally impossible hypotheses before the LLM spends its tool budget
on them.

## Demo

No API key, no Prometheus, no Slack. From a fresh clone:

```bash
pip install -e .
make demo
```

This boots the API with a preloaded mock LLM, fires the bundled
checkout-regression alert through the real webhook -> queue -> agent
pipeline, and prints the RCA:

```
RCA report 0193f4a4-2b87-7a31-9c1f-1d6a93dca8c1
Alert:       checkout p99 latency 2.18s (threshold 1.5s) for 5 min
Root cause:  payment  (confidence 0.40)
Reasoning:   The payment service began returning errors at 02:56:11Z, 18
             minutes before the checkout p99 alert. A PR merged at 02:55:42Z
             bumped the Stripe SDK from v7 to v8; ...
Evidence:
  - payment is the only ERROR-state node downstream of checkout  [tool_calls[0]]
  - A PR landed on payment 18 min before the alert  [tool_calls[1]]
  - payment is logging TypeError on charges.create  [tool_calls[2]]
  - payment error rate jumped from 0% to 87% at 02:56:11Z  [tool_calls[3]]
Action:      git revert abc1234 && deploy payment
```

The API stays up afterwards; point the web UI at it with
`cd web && npm install && npm run dev`.

## Quick start

```bash
# Backend
pip install -e ".[dev]"
pytest                                 # 299 tests
make eval                              # synthetic eval track, 6 cases

# FastAPI server
uvicorn ai_oncall.server:app --reload --port 8000
curl -H 'X-Tenant-Id: demo' http://localhost:8000/topology

# Frontend
cd web && npm install && npm run dev

# Full local stack (API + Prometheus + Loki)
docker compose up --build
```

To run against your own infrastructure (alert sources, Slack, telemetry,
runbooks), follow the setup checklist in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Eval results

Live single-shot RCA synthesis on the 6 synthetic fault families, scored
against hand-authored expected reports (run 2026-06-10):

| model | id | n | component match | top 3 accuracy | reason cosine | escalation precision | avg cost / case | avg latency / case | parse fails |
|---|---|---|---|---|---|---|---|---|---|
| claude-haiku | `claude-haiku-4-5-20251001` | 6 | 1.00 | 1.00 | 0.56 | 0.67 | $0.0015 | 2649ms | 0 |
| claude-sonnet | `claude-sonnet-4-6` | 6 | 1.00 | 1.00 | 0.58 | 0.67 | $0.0045 | 4739ms | 0 |
| claude-opus | `claude-opus-4-8` | 6 | 1.00 | 1.00 | 0.58 | 0.67 | $0.0089 | 3158ms | 0 |

What this measures: each model gets the alert plus a terse summary of the
expected investigation trail and produces a one-shot RCA. It evaluates
synthesis quality under perfect context, not the full tool-using loop,
so the comparison surface is identical across models. All three identify
the right component on all six cases; Haiku does it at a third of
Sonnet's cost, which is why it is the default. Reproduce with:

```bash
ANTHROPIC_API_KEY=... python -m evals.harness \
  --model-compare claude-haiku,claude-sonnet,claude-opus
```

The repo also ships 5 eval cases derived from public postmortems
(AWS 2021, Cloudflare 2022, Datadog 2023, GitHub 2018, Atlassian 2022)
plus loaders for the RCAEval RE3-OB and OpenRCA Bank benchmarks, and a
CI gate that fails on a > 5 pp metric drop. Details in
[docs/OPERATIONS.md](docs/OPERATIONS.md#eval-tracks).

## How it works

1. Receive an alert (PagerDuty / Grafana / Alertmanager / manual webhook),
   validate it against the JSON Schema, enqueue a durable job.
2. Build the causal dependency graph from observed OTel spans; fall back
   to `topology.yaml` when no spans are seen.
3. Plan 3-5 ranked hypotheses with the LLM, then prune any whose claimed
   root cause is unreachable from the alerting service in the graph.
4. Run up to 8 deterministic tool calls against telemetry, deploys,
   topology, and runbooks.
5. Synthesize a ranked RCA report against `schemas/rca_report.json`,
   correlate each hypothesis to the diff of the last deploy on its
   service, apply deterministic abstention rules, and stage remediation
   actions into approval tiers.
6. Post to Slack as Block Kit with an approve-rollback button for
   `propose`-tier actions; render to HTML; serve the dashboard.
7. Capture feedback reactions into `learnings.jsonl` and persist every
   report into a typed memory graph that future investigations query.

| Step | Module |
|---|---|
| RECEIVE + ENQUEUE | `ingest/alerts.py`, `ai_oncall/jobs/` |
| ASSEMBLE | `topology/builder.py`, `topology/from_spans.py` |
| PLAN + PRUNE | `agent/plan.py`, `agent/causal.py` |
| INVESTIGATE | `agent/investigate.py` (6 tools, 8-call cap) |
| SYNTHESIZE + CORRELATE + CALIBRATE + STAGE | `agent/{synthesize,correlation,calibration,staging}.py` |
| POST + ACTIONS + THREAD Q&A | `delivery/{slack,send,reactions,cd_dispatch,thread_qa,html}.py` |
| LEARN + REPLAY | `learnings/`, `agent/replay.py` |

Prompts pass through PII/secret redaction before reaching the model. All
storage rows carry `tenant_id` and the store filters on it at query time;
identity is the deployment's job, there is no login screen.

## Designed for trust

An agent that touches production has to earn the right to act. Four
mechanisms do that here.

### Calibrated abstention

Four deterministic rules in `agent/calibration.py` override the LLM's own
confidence when the evidence does not support a verdict: `cold_start` (no
telemetry baseline for the service), `confidence_floor` (top hypothesis
below threshold), `budget_exhausted` (the 8-call cap was hit before the
trail converged), and `two_strong_leads` (two hypotheses too close to
call). When a rule fires, the report escalates to a human instead of
guessing, and the top confidence is capped at 0.40 so Slack and the
dashboard render an unambiguous low-confidence state. The rules are code,
not prompt instructions; the model cannot talk its way past them.

### Staged actions behind an audit chain

Remediation never executes silently. Every action is staged into a tier:
`recommend` (text only), `propose` (a Slack approve button a human must
click), or `auto` (whitelisted runbooks only). An approved rollback is
HMAC-signed before it reaches your CD system, and refuses to send
unsigned. Every state-changing action is appended to `data/audit.jsonl`
with five governance fields (intent, contextual state, policy decision,
execution boundaries, actual outcome) linked by a SHA-256 hash chain:
`record_hash = sha256(prev_hash || canonical_json(fields))`. `ai-oncall
audit verify` re-walks the chain and reports the first broken index,
which detects insertion, deletion, edit, or reordering without separate
signing infrastructure.

### Mistakes become regression tests

A thumbs-down or "wrong root cause" reaction in Slack is not just logged.
`ai-oncall feedback-export` turns each negative reaction into an eval
fixture, so the exact incident the agent got wrong joins the regression
suite. `ai-oncall replay <report_id>` re-runs the full pipeline on any
stored incident and diffs the result against the original (verdict:
match, drift, regression, improvement). It exits non-zero on regression
(`--fail-on-regression`, batch mode via `--batch-from`), built to run in
CI against a curated incident list so a prompt or model change that
quietly degrades one fault family fails the build instead of shipping.

### Memory with trust tiers

Every report is persisted into a typed memory graph: `(tenant, service)
-> root_cause_class` counts that give `get_past_incidents` a per-service
prior ("payment has seen 4 deploy regressions this quarter"). Rows carry
a trust tier — `local` (this tenant only, the default), `aggregated`
(opted into cross-tenant priors), `verified` (a human confirmed the RCA)
— so cross-tenant learning is opt-in and human-confirmed signal is
distinguishable from the agent's own guesses.

## Repo layout

```
ai-oncall/
  ai_oncall/        pipeline stages, delivery, storage drivers, jobs, llm
  schemas/          JSON Schemas (alert, plan, report, topology, ...)
  evals/            harness, scoring, model compare, benchmark loaders
  evals/cases/      6 synthetic + 5 real-postmortem cases
  fixtures/         synthetic alerts + hand-authored expected reports
  web/              Next.js dashboard (incidents, trace, topology)
  tests/            contracts (schema round-trip), integration, unit
  docs/             BRIEF (product contract), OPERATIONS, UI_DESIGN,
                    integrations, KICKOFF
  deploy/           Slack app manifest, Prometheus config
```

## Documentation

- [docs/OPERATIONS.md](docs/OPERATIONS.md): setup checklist, deploy,
  Slack configuration, audit chain, trust tiers, replay, eval tracks.
- [docs/integrations.md](docs/integrations.md): copy-paste webhook
  configs for Alertmanager, Grafana, and PagerDuty.
- [docs/BRIEF.md](docs/BRIEF.md): the product contract. Read before
  changing behavior.
- [docs/UI_DESIGN.md](docs/UI_DESIGN.md): the visual contract for the
  web UI.

## License

MIT, see [LICENSE](LICENSE).
