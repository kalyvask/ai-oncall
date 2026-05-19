// Incident detail.
// Pulls /incidents/{id} from the FastAPI backend at render time. Maps the
// RcaReport shape into the existing UI: hairline-anchored top hypothesis,
// action layer with blast-radius pill and approval flow, tabbed
// trace/topology/runbook/alternatives panel.

import { Pill } from "@/components/Pill";
import { Statline } from "@/components/Statline";
import { IncidentActions } from "@/components/IncidentActions";
import { api, type Hypothesis, type IncidentDetail, type StagedAction, type ToolCall } from "@/lib/api";
import { CATALOG_RATES } from "@/lib/cost";

type Props = { params: Promise<{ id: string }>; searchParams: Promise<{ tab?: string }> };

const TABS = ["trace", "topology", "runbook", "alternatives"] as const;

const BLAST_TONE = { low: "pos", medium: "warn", high: "neg" } as const;

function estimateCost(modelId: string | undefined, tokensIn: number, tokensOut: number): number {
  if (!modelId) return 0;
  const rates = CATALOG_RATES[modelId];
  if (!rates) return 0;
  return (tokensIn * rates.in + tokensOut * rates.out) / 1_000_000;
}

function blastRadiusFor(action: StagedAction | null | undefined): "low" | "medium" | "high" {
  if (!action) return "medium";
  if (action.blast_radius) return action.blast_radius;
  if (action.tier === "auto") return "low";
  if (action.tier === "recommend") return "high";
  return "medium";
}

function deriveActionLayer(top: Hypothesis | undefined) {
  const sa = top?.staged_action ?? null;
  return {
    blast_radius: blastRadiusFor(sa),
    executor: sa?.tier === "auto" ? "auto-runner" : "approval queue",
    command: sa?.command ?? top?.recommended_action ?? "No staged action.",
    estimated_seconds: 90,
    rollback_seconds: 60,
    approval_threshold: sa?.approval_threshold ?? 0.85,
    approvers_required: sa?.tier === "auto" ? 0 : 1,
    tier: sa?.tier ?? "recommend",
  };
}

export default async function IncidentDetail({ params, searchParams }: Props) {
  const { id } = await params;
  const { tab: rawTab } = await searchParams;
  const tab = (TABS as readonly string[]).includes(rawTab || "") ? rawTab! : "trace";

  let incident: IncidentDetail | null = null;
  let fetchError: string | null = null;
  try {
    incident = await api.incident(id);
  } catch (e) {
    fetchError = e instanceof Error ? e.message : String(e);
  }

  if (fetchError || !incident) {
    return (
      <div className="flex flex-col gap-6">
        <h1 className="text-2xl font-medium text-ink-0">Incident not found</h1>
        <p className="max-w-prose text-sm text-rose-400">{fetchError ?? "Unknown error."}</p>
      </div>
    );
  }

  const top: Hypothesis | undefined = incident.hypotheses[0];
  const alternatives = incident.hypotheses.slice(1);
  const a = deriveActionLayer(top);
  const meetsThreshold = (top?.confidence ?? 0) >= a.approval_threshold && a.tier === "propose";

  const tool_calls: ToolCall[] = incident.investigation?.tool_calls ?? [];
  const tokens_in = incident.investigation?.tokens_in ?? 0;
  const tokens_out = incident.investigation?.tokens_out ?? 0;
  const cost = estimateCost(incident.model?.id, tokens_in, tokens_out);
  const modelLabel = incident.model?.id?.split("-").slice(0, 3).join("-") ?? "unknown";

  return (
    <div className="flex flex-col gap-12">
      <header className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <Pill tone={incident.alert.severity === "page" ? "neg" : incident.alert.severity === "warn" ? "warn" : "neutral"}>
            {incident.alert.severity}
          </Pill>
          <span className="eyebrow">incident · {id.slice(0, 8)}</span>
        </div>
        <h1 className="max-w-prose text-2xl font-medium tracking-tight text-ink-0">
          {incident.alert.title}
        </h1>
        <p className="text-sm text-ink-3">
          <code className="text-ink-2">{incident.alert.service}</code>
          <span className="mx-2">·</span>
          <time>{incident.alert.fired_at}</time>
        </p>
      </header>

      <section className="border-y border-ink-7 py-8">
        <div className="grid grid-cols-1 gap-8 md:grid-cols-[1fr_auto]">
          <div className="flex flex-col gap-5">
            <p className="eyebrow">Top hypothesis</p>
            <h2 className="font-mono text-2xl text-ink-0">{top?.root_cause_service ?? "—"}</h2>
            <p className="max-w-prose text-base text-ink-1">{top?.reasoning ?? top?.statement ?? ""}</p>
            {top?.evidence && top.evidence.length > 0 && (
              <ul className="flex flex-col gap-2 text-sm text-ink-2">
                {top.evidence.map((e, i) => (
                  <li key={i} className="flex gap-3">
                    <span className="font-mono text-xs tabular-nums text-ink-4">
                      {(i + 1).toString().padStart(2, "0")}
                    </span>
                    <span className="flex-1">{e.claim}</span>
                    <code className="text-xs text-ink-4">{e.source}</code>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="flex flex-col items-start gap-1 md:items-end">
            <p className="eyebrow">Confidence</p>
            <p className="font-mono text-3xl font-normal tabular-nums text-acc">
              {Math.round((top?.confidence ?? 0) * 100)}%
            </p>
            <p className="text-xs text-ink-3">model {modelLabel}</p>
          </div>
        </div>
      </section>

      <section aria-labelledby="action-heading" className="flex flex-col gap-5">
        <div className="flex items-baseline justify-between">
          <div className="flex items-center gap-3">
            <h2 id="action-heading" className="text-lg font-medium text-ink-0">Prepared action</h2>
            <Pill tone={BLAST_TONE[a.blast_radius]}>{a.blast_radius} blast radius</Pill>
            <Pill tone="neutral">tier: {a.tier}</Pill>
          </div>
          <span className="eyebrow">via {a.executor}</span>
        </div>

        <div className="grid grid-cols-1 gap-5 md:grid-cols-[1fr_auto]">
          <pre className="overflow-x-auto rounded-md bg-ink-9 px-4 py-3 font-mono text-sm text-ink-1 ring-1 ring-ink-7">
            {a.command}
          </pre>
          <IncidentActions
            reportId={incident.report_id}
            approveEnabled={meetsThreshold}
            approveLabel={meetsThreshold ? "Approve and run" : a.tier === "recommend" ? "Recommend only" : "Below threshold"}
          />
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
          Action Staging is human-in-the-loop. Tier <code>recommend</code> is read-only;
          <code>propose</code> shows the Approve button when confidence ≥ threshold;
          <code>auto</code> requires no approval and executes via the auto-runner.
        </p>
      </section>

      <section className="grid grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-4">
        <Statline label="Generated" value={new Date(incident.generated_at).toLocaleTimeString()} />
        <Statline
          label="Tokens"
          value={(tokens_in + tokens_out).toLocaleString()}
          delta={`in ${tokens_in} / out ${tokens_out}`}
        />
        <Statline label="Cost" value={cost > 0 ? `$${cost.toFixed(4)}` : "—"} />
        <Statline label="Tool calls" value={String(tool_calls.length)} />
      </section>

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
              {tool_calls.map((c, i) => (
                <li
                  key={i}
                  className="grid grid-cols-[auto_auto_1fr_auto] items-baseline gap-5 border-t border-ink-7 py-3 first:border-t-0"
                >
                  <span className="font-mono text-xs tabular-nums text-ink-4">
                    {(i + 1).toString().padStart(2, "0")}
                  </span>
                  <code className="text-sm text-acc-hi">{c.tool}</code>
                  <p className="text-sm text-ink-2">{c.result_summary}</p>
                  <span className="font-mono text-xs tabular-nums text-ink-3">
                    {Math.round(c.duration_ms)}ms
                  </span>
                </li>
              ))}
              {tool_calls.length === 0 && (
                <li className="text-sm text-ink-3">No tool calls recorded for this incident.</li>
              )}
            </ol>
          )}

          {tab === "topology" && (
            <p className="max-w-prose text-sm text-ink-2">
              Topology view lives at{" "}
              <a className="text-acc-hi underline-offset-2 hover:underline" href="/topology">
                /topology
              </a>
              . The graph is rebuilt from observed OTel spans (10-minute window),
              falling back to <code>topology.yaml</code>.
            </p>
          )}

          {tab === "runbook" && (
            <p className="max-w-prose text-sm text-ink-2">
              Linked runbook: <code className="text-acc-hi">runbooks/{top?.root_cause_service ?? "unknown"}.md</code>.
              Full runbooks at{" "}
              <a className="text-acc-hi underline-offset-2 hover:underline" href="/runbooks">
                /runbooks
              </a>
              .
            </p>
          )}

          {tab === "alternatives" && (
            <ul className="flex flex-col" role="list">
              {alternatives.map((h, i) => (
                <li key={i} className="grid grid-cols-[auto_1fr_auto] items-baseline gap-5 border-t border-ink-7 py-4 first:border-t-0">
                  <code className="text-sm text-ink-1">{h.root_cause_service}</code>
                  <div>
                    <p className="text-sm text-ink-2">{h.reasoning ?? h.statement}</p>
                    {h.recommended_action && <p className="mt-1 text-xs text-ink-4">{h.recommended_action}</p>}
                  </div>
                  <span className="font-mono text-sm tabular-nums text-ink-3">
                    {Math.round(h.confidence * 100)}%
                  </span>
                </li>
              ))}
              {alternatives.length === 0 && (
                <li className="text-sm text-ink-3">No alternative hypotheses.</li>
              )}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
