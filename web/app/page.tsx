// Incidents inbox. Fetches /incidents from the FastAPI backend at render
// time. Rows map from the IncidentRow shape returned by the API. When the
// backend is unreachable, falls back to an empty state with the configured
// onboarding hint.

import Link from "next/link";
import { Pill } from "@/components/Pill";
import { api, type IncidentRow } from "@/lib/api";

type Blast = "low" | "medium" | "high";
type Severity = "page" | "warn" | "info";
type Row = {
  id: string;
  service: string;
  severity: Severity;
  triggered_at: string;
  hypothesis: string;
  confidence: number;
  action: { command: string; blast: Blast; threshold: number };
};

const BLAST_TONE: Record<Blast, "pos" | "warn" | "neg"> = { low: "pos", medium: "warn", high: "neg" };

const SEV_TONE: Record<Severity, "neg" | "warn" | "neutral"> = {
  page: "neg",
  warn: "warn",
  info: "neutral",
};

const ago = (iso: string): string => {
  const ms = Date.now() - Date.parse(iso);
  const h = Math.floor(ms / 3.6e6);
  if (h < 1) return `${Math.floor(ms / 6e4)}m ago`;
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
};

function severityForConfidence(top_confidence: number, abstained: boolean): Severity {
  if (abstained) return "info";
  if (top_confidence >= 0.85) return "page";
  if (top_confidence >= 0.6) return "warn";
  return "info";
}

function mapIncident(r: IncidentRow): Row {
  return {
    id: r.report_id,
    service: r.service,
    severity: severityForConfidence(r.top_confidence, r.abstained),
    triggered_at: r.created_at,
    hypothesis: r.root_cause_class
      ? `${r.root_cause_class} in ${r.root_cause_service}`
      : `root cause in ${r.root_cause_service}`,
    confidence: r.top_confidence,
    action: { command: "", blast: "medium", threshold: 0.85 },
  };
}

export default async function IncidentsPage() {
  let rows: Row[] = [];
  let tenant = "demo";
  let fetchError: string | null = null;
  try {
    const data = await api.incidents(50);
    tenant = data.tenant_id;
    rows = data.items.map(mapIncident);
  } catch (e) {
    fetchError = e instanceof Error ? e.message : String(e);
  }

  const pages = rows.filter((r) => r.severity === "page");
  const others = rows.filter((r) => r.severity !== "page");
  const actionable = rows.filter((r) => r.confidence >= 0.85).length;

  return (
    <div className="flex flex-col gap-12">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="eyebrow mb-3">Open incidents</p>
          <h1 className="font-serif text-3xl font-normal tracking-tight text-ink-0">
            {rows.length} incidents
            <span className="text-ink-3"> · </span>
            <span className="text-acc">{actionable} actionable</span>
          </h1>
        </div>
        <dl className="flex gap-8 text-sm">
          <div>
            <dt className="eyebrow">Pages</dt>
            <dd className="mt-1 font-mono text-lg tabular-nums text-ink-0">{pages.length}</dd>
          </div>
          <div>
            <dt className="eyebrow">Warnings</dt>
            <dd className="mt-1 font-mono text-lg tabular-nums text-ink-0">{others.length}</dd>
          </div>
          <div>
            <dt className="eyebrow">Tenant</dt>
            <dd className="mt-1 font-mono text-lg tabular-nums text-ink-0">{tenant}</dd>
          </div>
        </dl>
      </header>

      {fetchError && (
        <p className="max-w-prose text-sm text-rose-400">
          Failed to reach the API: {fetchError}. Is the FastAPI server running on{" "}
          <code>{process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000"}</code>?
        </p>
      )}

      {pages.length > 0 && (
        <section>
          <h2 className="eyebrow mb-3">Paging now</h2>
          <ul className="divide-y divide-ink-7 border-y border-ink-7" role="list">
            {pages.map((r) => (
              <IncidentRow key={r.id} row={r} />
            ))}
          </ul>
        </section>
      )}

      {others.length > 0 && (
        <section>
          <h2 className="eyebrow mb-3">Warnings &amp; info</h2>
          <ul className="divide-y divide-ink-7 border-y border-ink-7" role="list">
            {others.map((r) => (
              <IncidentRow key={r.id} row={r} />
            ))}
          </ul>
        </section>
      )}

      {!fetchError && rows.length === 0 && (
        <p className="max-w-prose text-ink-3">
          No incidents yet. POST an alert to <code>/webhooks/alert</code> with{" "}
          <code>X-Tenant-Id: {tenant}</code> to trigger the pipeline.
        </p>
      )}
    </div>
  );
}

function IncidentRow({ row }: { row: Row }) {
  const ready = row.confidence >= row.action.threshold;
  return (
    <li>
      <Link
        href={`/incidents/${row.id}`}
        className="grid grid-cols-[auto_7rem_1fr_auto_auto] items-center gap-5 py-4 transition-colors duration-fast hover:bg-ink-8"
      >
        <Pill tone={SEV_TONE[row.severity]}>{row.severity}</Pill>
        <code className="text-sm text-ink-1">{row.service}</code>
        <p className="line-clamp-1 text-sm text-ink-1">{row.hypothesis}</p>
        <div className="hidden items-center gap-2 md:flex">
          <Pill tone={BLAST_TONE[row.action.blast]}>{row.action.blast}</Pill>
          <span className={`text-xs ${ready ? "text-acc-hi" : "text-ink-3"}`}>
            {ready ? "action ready" : "below threshold"}
          </span>
        </div>
        <div className="flex items-baseline gap-4 text-right">
          <time className="hidden text-xs tabular-nums text-ink-3 sm:inline">{ago(row.triggered_at)}</time>
          <span className="font-mono text-sm tabular-nums text-acc-hi">
            {Math.round(row.confidence * 100)}%
          </span>
        </div>
      </Link>
    </li>
  );
}
