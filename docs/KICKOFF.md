# KICKOFF — paste this into a fresh Claude Code session

Everything below is the single message you send first. The four attached
files in this directory (`BRIEF.md`, `schemas/rca_report.json`,
`fixtures/synthetic_alerts/checkout_regression.json`,
`fixtures/expected_reports/checkout_regression.json`,
`runbooks/checkout.md`) are the only reference material the agent needs.

Open Claude Code in an empty target directory, then either:

1. Copy the entire `ai-oncall-brief/` directory next to the target repo and
   start the session with the prompt below. Or:
2. Paste the prompt below and attach the five files via Claude Code's file
   reference (`@path/to/file`).

---

## The prompt

> You are building **`ai-oncall`** (placeholder name — confirm with me
> before the first commit), a single-process Python service plus a Next.js
> web UI that diagnoses production incidents using an LLM.
>
> **Read `BRIEF.md` end to end before writing any code.** It is your
> contract for this entire build. The brief defines:
>
> 1. The user (Sam) and the 60-second win condition.
> 2. The seven inputs the agent reasons over (alert, OTLP traces /
>    metrics / logs, GitHub deploys, live topology, runbooks).
> 3. The seven-stage pipeline: RECEIVE → ASSEMBLE → PLAN → INVESTIGATE →
>    SYNTHESIZE → POST → LEARN. Stages 3 and 4 are a **tool-using
>    investigation loop** — the LLM proposes hypotheses and queries, calls
>    tools, refines. This is the technical bet.
> 4. The six tools the LLM gets (`query_metrics`, `query_logs`,
>    `get_recent_deploys`, `get_runbook`, `get_topology`,
>    `get_past_incidents`). Hard cap of 8 tool calls per incident.
> 5. The schemas — write these first. `schemas/rca_report.json` in this
>    directory is the canonical output shape.
> 6. The eval harness — three tracks (synthetic, RCAEval RE3-OB, OpenRCA
>    Bank), four metrics (component match, reason cosine, trajectory
>    score, escalation precision). Build the eval *before* the agent.
> 7. The four delivery surfaces: Slack (Block Kit, primary), Next.js web
>    UI at `web/` (App Router, TypeScript, Tailwind), CLI, static HTML
>    export.
> 8. Multi-tenancy via `X-Tenant-Id` header + row-level filter. No auth,
>    no RBAC.
> 9. The pluggable telemetry store: SQLite (dev), DuckDB (single-node
>    prod), Snowflake (multi-tenant prod, stub in v1).
> 10. The repo skeleton, the strict order of work (10 numbered steps), and
>     the explicit forbiddens.
>
> **Reference fixtures** (use as your first eval target and your shape
> ground-truth):
>
> - `fixtures/synthetic_alerts/checkout_regression.json` — example input.
> - `fixtures/expected_reports/checkout_regression.json` — hand-authored
>   expected output for that input. Your eval should produce something
>   close to this with `claude-haiku` and exactly this with `MockLlm`
>   (deterministic stub).
> - `runbooks/checkout.md` — example runbook so the retrieval path has
>   content from day one.
>
> **Hard rules.**
>
> - Never copy code from any external repo. If you reference OpenRCA or
>   RCAEval, read the upstream docs and reimplement.
> - No `--no-verify`, no skipping hooks. Pre-commit + ruff + mypy +
>   pytest must pass on every commit.
> - Structured logging from line one (`logging` module, optional JSON via
>   `LOG_JSON=1`). No `print` calls.
> - Token budget enforced on every prompt; oldest evidence drops first.
> - Hard $0.50 cost ceiling per RCA. Default model is `claude-haiku`;
>   `claude-opus` is opt-in via `RCA_MODEL`.
> - Multi-tenancy is a Day 1 requirement. Every record carries
>   `tenant_id`. Every API endpoint enforces `X-Tenant-Id`.
>
> **Order of work** (do not start step N+1 until step N's eval passes):
>
> 1. JSON schemas + Pydantic models. Round-trip tests for every fixture.
> 2. Eval harness with `MockLlm`. Five synthetic scenarios (the
>    `checkout_regression` fixture is one of them; design four more).
>    `make eval` green.
> 3. `TelemetryStore` interface + SQLite + DuckDB drivers. Snowflake stub
>    raises `NotImplementedError` with a clear message.
> 4. Stages 1–2 (RECEIVE, ASSEMBLE) end-to-end against fixtures, all
>    tenant-scoped.
> 5. Stage 5 (SYNTHESIZE) single-shot baseline. Record metrics.
> 6. Stages 3–4 (PLAN + INVESTIGATE) tool-using loop. Measure delta vs.
>    baseline.
> 7. Slack delivery + reaction-driven feedback loop.
> 8. Next.js UI scaffold: incidents list, incident detail (with reasoning
>    trace), topology, runbooks, settings. Polls REST; carries
>    `X-Tenant-Id`.
> 9. OpenRCA + RCAEval loaders. Real-data eval. Document gaps.
> 10. Snowflake driver — only when a customer needs it. Until then, stub.
>
> **Stop and ask before:**
>
> - choosing the final product name,
> - picking the default model and the cost ceiling,
> - naming the GitHub repo,
> - adding any new top-level dependency beyond what's in the brief.
>
> Begin with step 1: read `BRIEF.md` and `schemas/rca_report.json` in
> full, then propose the complete schema set (alert, telemetry_record,
> topology_snapshot, change_event, investigation_plan, rca_report) as
> JSON-schema files plus Pydantic models. Show me the schemas before
> generating code that uses them.

---

## What's already in this directory

```
ai-oncall-brief/
  KICKOFF.md                                            ← you are here
  BRIEF.md                                              full design brief
  UI_DESIGN.md                                          UI design language
  schemas/
    rca_report.json                                     canonical output schema
  fixtures/
    synthetic_alerts/checkout_regression.json           example input
    expected_reports/checkout_regression.json           expected output
  runbooks/
    checkout.md                                         example runbook
```

> When you reach **step 8** (Next.js UI scaffold), read `UI_DESIGN.md` end
> to end before writing any JSX. It is non-negotiable: OKLCH-only colors,
> banned fonts list, banned anti-patterns, the 8-item pre-merge checklist.
> The references are `ryanthedev/design-for-ai` (intentional proportions,
> warm/cool color balance) and `pbakaus/impeccable` (fight default-AI UI:
> no Inter, no purple gradients, no nested cards, no gray-on-color).

## Optional next moves

- Replace `ai-oncall` with the real product name everywhere before you
  paste the prompt. Search the four files above for the literal string.
- Decide on the cost ceiling and default model now — those are the two
  questions the agent will ask first if you don't pre-answer them.
- Drop a `LICENSE` (MIT) and a `CREDITS.md` (mention prior team work if
  applicable) into the target repo before the first commit lands.
