// Incident detail.
//   Dominant element (squint test) is the top-hypothesis block — anchored
//   by hairline rules above and below, NO side-stripe border (banned).
//   The ACTION LAYER preview lives directly under the hypothesis: a single
//   prepared remediation, classified by blast radius, with an Approve button
//   and an Escalate / Skip pair. Mirrors the Action Staging pattern from
//   the observability primer (low/medium/high blast radius, per-class
//   approval threshold). Wire-up to a real executor is the next layer.

import { Pill } from "@/components/Pill";
import { Statline } from "@/components/Statline";

type Props = { params: Promise<{ id: string }>; searchParams: Promise<{ tab?: string }> };

const TABS = ["trace", "topology", "runbook", "alternatives"] as const;

const REPORT = {
  alert: {
    title: "checkout p99 latency 2.18s, threshold 1.5s, for 5 min",
    service: "checkout",
    severity: "page" as const,
    fired_at: "2026-04-25T03:14:22Z",
  },
  model: { id: "claude-haiku-4-5-20251001" },
  investigation: {
    tokens_in: 4812,
    tokens_out: 612,
    cost_usd: 0.018,
    tool_calls: [
      { tool: "get_topology", input: { service: "checkout", depth: 2 }, summary: "checkout depends on cart, payment, currency, shipping. payment is in ERROR.", duration_ms: 12 },
      { tool: "get_recent_deploys", input: { service: "payment", since: "2026-04-24T03:14:00Z" }, summary: "1 PR merged 18 min before alert: 'bump stripe SDK 7 to 8'", duration_ms: 184 },
      { tool: "query_logs", input: { service: "payment", regex: "TypeError|charges.create" }, summary: "21 ERROR lines: TypeError on charges.create signature mismatch", duration_ms: 96 },
      { tool: "query_metrics", input: { service: "payment", metric: "http.server.error_rate", agg: "rate" }, summary: "error rate 0% baseline, jumped to 87% at 02:56:11Z", duration_ms: 71 },
    ],
  },
  top: {
    root_cause_service: "payment",
    confidence: 0.92,
    reasoning:
      "The payment service began returning errors at 02:56:11Z, 18 minutes before the checkout p99 alert. A PR merged at 02:55:42Z (commit abc1234) bumped the Stripe SDK from v7 to v8; the v8 client.charges.create signature changed. payment logs show 21 TypeError lines matching the new signature.",
    evidence: [
      { claim: "payment is the only ERROR-state node downstream of checkout", source: "tool_calls[0]" },
      { claim: "PR landed on payment 18 min before the alert", source: "tool_calls[1]" },
      { claim: "21 TypeError lines on charges.create in the window", source: "tool_calls[2]" },
      { claim: "payment error rate jumped 0% to 87% at 02:56:11Z", source: "tool_calls[3]" },
    ],
    runbook_link: "runbooks/payment.md",
  },
  // --- structured Action Staging envelope (the layer the user asked about) ---
  action: {
    blast_radius: "medium" as const,
    executor: "github + ci",
    command: "git revert abc1234 && deploy payment",
    estimated_seconds: 90,
    rollback_seconds: 60,
    approval_threshold: 0.90,
    approvers_required: 1,
  },
  alternatives: [
    {
      root_cause_service: "checkout",
      confidence: 0.18,
      reasoning: "checkout is the alerting service but its code is unchanged; latency is a downstream symptom of payment retries.",
      action: "Do not roll back checkout. payment is the cause.",
    },
    {
      root_cause_service: "stripe",
      confidence: 0.05,
      reasoning: "External Stripe outage could in theory cause this, but the TypeError signature is client-side, not 5xx.",
      action: "Check status.stripe.com only if rollback does not resolve.",
    },
  ],
};

const BLAST_TONE = { low: "pos", medium: "warn", high: "neg" } as const;

export default async function IncidentDetail({ params, searchParams }: Props) {
  const { id } = await params;
  const { tab: rawTab } = await searchParams;
  const tab = (TABS as readonly string[]).includes(rawTab || "") ? rawTab! : "trace";
  const a = REPORT.action;
  const meetsThreshold = REPORT.top.confidence >= a.approval_threshold;

  return (
    <div className="flex flex-col gap-12">
      {/* HEADER — left-anchored, asymmetric, no center alignment. */}
      <header className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <Pill tone="neg">{REPORT.alert.severity}</Pill>
          <span className="eyebrow">incident · {id.slice(0, 8)}</span>
        </div>
        <h1 className="max-w-prose text-2xl font-medium tracking-tight text-ink-0">
          {REPORT.alert.title}
        </h1>
        <p className="text-sm text-ink-3">
          <code className="text-ink-2">{REPORT.alert.service}</code>
          <span className="mx-2">·</span>
          <time>{REPORT.alert.fired_at}</time>
        </p>
      </header>

      {/* DOMINANT ELEMENT — top hypothesis. No card; hairline rules + scale. */}
      <section className="border-y border-ink-7 py-8">
        <div className="grid grid-cols-1 gap-8 md:grid-cols-[1fr_auto]">
          <div className="flex flex-col gap-5">
            <p className="eyebrow">Top hypothesis</p>
            <h2 className="font-mono text-2xl text-ink-0">{REPORT.top.root_cause_service}</h2>
            <p className="max-w-prose text-base text-ink-1">{REPORT.top.reasoning}</p>
            <ul className="flex flex-col gap-2 text-sm text-ink-2">
              {REPORT.top.evidence.map((e, i) => (
                <li key={i} className="flex gap-3">
                  <span className="font-mono text-xs tabular-nums text-ink-4">{(i + 1).toString().padStart(2, "0")}</span>
                  <span className="flex-1">{e.claim}</span>
                  <code className="text-xs text-ink-4">{e.source}</code>
                </li>
              ))}
            </ul>
          </div>
          <div className="flex flex-col items-start gap-1 md:items-end">
            <p className="eyebrow">Confidence</p>
            <p className="font-mono text-3xl font-normal tabular-nums text-acc">
              {Math.round(REPORT.top.confidence * 100)}%
            </p>
            <p className="text-xs text-ink-3">model {REPORT.model.id.split("-").slice(0, 3).join("-")}</p>
          </div>
        </div>
      </section>

      {/* ACTION LAYER — the structured remediation envelope.
          Blast radius classification, threshold-gated approval button,
          executor binding. Wire-up is stubbed pending the BRIEF.md §13
          decisions (name, model, cost ceiling). */}
      <section aria-labelledby="action-heading" className="flex flex-col gap-5">
        <div className="flex items-baseline justify-between">
          <div className="flex items-center gap-3">
            <h2 id="action-heading" className="text-lg font-medium text-ink-0">Prepared action</h2>
            <Pill tone={BLAST_TONE[a.blast_radius]}>{a.blast_radius} blast radius</Pill>
          </div>
          <span className="eyebrow">via {a.executor}</span>
        </div>

        <div className="grid grid-cols-1 gap-5 md:grid-cols-[1fr_auto]">
          <pre className="overflow-x-auto rounded-md bg-ink-9 px-4 py-3 font-mono text-sm text-ink-1 ring-1 ring-ink-7">
            {a.command}
          </pre>
          <div className="flex items-stretch gap-2">
            <button
              type="button"
              disabled={!meetsThreshold}
              className="rounded-md bg-acc px-5 py-2 text-sm font-semibold text-ink-9 transition-colors duration-fast hover:bg-acc-hi disabled:cursor-not-allowed disabled:bg-ink-7 disabled:text-ink-4"
            >
              {meetsThreshold ? "Approve and run" : "Below threshold"}
            </button>
            <button
              type="button"
              className="rounded-md border border-ink-7 px-4 py-2 text-sm text-ink-1 transition-colors duration-fast hover:border-ink-5 hover:bg-ink-8"
            >
              Escalate
            </button>
            <button
              type="button"
              className="rounded-md border border-ink-7 px-4 py-2 text-sm text-ink-3 transition-colors duration-fast hover:border-ink-5 hover:bg-ink-8"
            >
              Skip
            </button>
          </div>
        </div>

        <dl className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm sm:grid-cols-4">
          <div>
            <dt className="eyebrow">ETA</dt>
            <dd className="mt-1 font-mono tabular-nums text-ink-1">{a.estimated_seconds}s</dd>
          </div>
          <div>
            <dt className="eyebrow">Rollback</dt>
            <dd className="mt-1 font-mono tabular-nums text-ink-1">{a.rollback_seconds}s</dd>
          </div>
          <div>
            <dt className="eyebrow">Threshold</dt>
            <dd className="mt-1 font-mono tabular-nums text-ink-1">≥ {Math.round(a.approval_threshold * 100)}%</dd>
          </div>
          <div>
            <dt className="eyebrow">Approvers</dt>
            <dd className="mt-1 font-mono tabular-nums text-ink-1">{a.approvers_required}</dd>
          </div>
        </dl>

        <p className="max-w-prose text-xs text-ink-4">
          Action Staging is human-in-the-loop. Approval routes execution to {a.executor}; rollback is automatic on failure. Auto-execute is opt-in per blast-radius class.
        </p>
      </section>

      {/* STATLINE CLUSTER — quiet metadata strip. */}
      <section className="grid grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-4">
        <Statline label="Time to RCA" value="26s" />
        <Statline
          label="Tokens"
          value={(REPORT.investigation.tokens_in + REPORT.investigation.tokens_out).toLocaleString()}
          delta={`in ${REPORT.investigation.tokens_in} / out ${REPORT.investigation.tokens_out}`}
        />
        <Statline label="Cost" value={`$${REPORT.investigation.cost_usd.toFixed(3)}`} />
        <Statline label="Tool calls" value={String(REPORT.investigation.tool_calls.length)} />
      </section>

      {/* TABS — URL-driven. Underline-on-active, no chip pills. */}
      <section>
        <nav role="tablist" className="flex gap-6 border-b border-ink-7">
          {TABS.map((t) => {
            const active = tab === t;
            return (
              <a
                key={t}
                href={`?tab=${t}`}
                role="tab"
                aria-selected={active}
                className={`-mb-px border-b-2 px-1 py-2 text-sm capitalize transition-colors duration-fast ${
                  active
                    ? "border-acc text-ink-0"
                    : "border-transparent text-ink-3 hover:text-ink-1"
                }`}
              >
                {t}
              </a>
            );
          })}
        </nav>

        <div className="pt-6">
          {tab === "trace" && (
            <ol className="flex flex-col" role="list">
              {REPORT.investigation.tool_calls.map((c, i) => (
                <li
                  key={i}
                  className="grid grid-cols-[auto_auto_1fr_auto] items-baseline gap-5 border-t border-ink-7 py-3 first:border-t-0"
                >
                  <span className="font-mono text-xs tabular-nums text-ink-4">
                    {(i + 1).toString().padStart(2, "0")}
                  </span>
                  <code className="text-sm text-acc-hi">{c.tool}</code>
                  <p className="text-sm text-ink-2">{c.summary}</p>
                  <span className="font-mono text-xs tabular-nums text-ink-3">
                    {c.duration_ms.toFixed(0)}ms
                  </span>
                </li>
              ))}
            </ol>
          )}

          {tab === "topology" && (
            <p className="max-w-prose text-sm text-ink-2">
              Topology view lives at <a className="text-acc-hi underline-offset-2 hover:underline" href="/topology">/topology</a>.
              The relevant subgraph for this incident: <code>checkout to cart, payment, currency, shipping</code>; only payment is in ERROR.
            </p>
          )}

          {tab === "runbook" && (
            <p className="max-w-prose text-sm text-ink-2">
              Linked runbook: <code className="text-acc-hi">{REPORT.top.runbook_link}</code>.
              Full runbooks at <a className="text-acc-hi underline-offset-2 hover:underline" href="/runbooks">/runbooks</a>.
            </p>
          )}

          {tab === "alternatives" && (
            <ul className="flex flex-col" role="list">
              {REPORT.alternatives.map((a, i) => (
                <li key={i} className="grid grid-cols-[auto_1fr_auto] items-baseline gap-5 border-t border-ink-7 py-4 first:border-t-0">
                  <code className="text-sm text-ink-1">{a.root_cause_service}</code>
                  <div>
                    <p className="text-sm text-ink-2">{a.reasoning}</p>
                    <p className="mt-1 text-xs text-ink-4">{a.action}</p>
                  </div>
                  <span className="font-mono text-sm tabular-nums text-ink-3">
                    {Math.round(a.confidence * 100)}%
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
