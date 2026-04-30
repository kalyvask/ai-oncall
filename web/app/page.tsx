// Incidents inbox.
//   Composition: editorial Fraunces hero ("4 incidents · 1 actionable now")
//   anchored left, system summary metadata right. Asymmetric.
//   Rows: NO side-stripe border (impeccable absolute ban). Severity carried
//   in a Pill with a colored dot pip — color + shape, not color alone.
//   Rhythm: tight gap between rows in the same severity bucket; generous
//   gap before the lower-severity bucket.

import Link from "next/link";
import { Pill } from "@/components/Pill";

type Blast = "low" | "medium" | "high";
type Row = {
  id: string;
  service: string;
  severity: "page" | "warn" | "info";
  triggered_at: string;
  hypothesis: string;
  confidence: number;
  action: { command: string; blast: Blast; threshold: number };
};

const ROWS: Row[] = [
  {
    id: "0193f4a4-2b87-7a31-9c1f-1d6a93dca8c1",
    service: "checkout",
    severity: "page",
    triggered_at: "2026-04-25T03:14:22Z",
    hypothesis: "payment SDK regression after stripe v7 to v8 upgrade",
    confidence: 0.92,
    action: { command: "git revert abc1234 && deploy payment", blast: "medium", threshold: 0.90 },
  },
  {
    id: "0193f4b1-1c44-7b22-ad80-2e7b04ed91d2",
    service: "cart",
    severity: "page",
    triggered_at: "2026-04-26T14:02:11Z",
    hypothesis: "cart-db pool saturated at 100%",
    confidence: 0.86,
    action: { command: "Bump cart-db pool size; restart largest cart pod", blast: "low", threshold: 0.80 },
  },
  {
    id: "0193f4c2-2d55-7c33-be91-3f8c15fe02e3",
    service: "search",
    severity: "warn",
    triggered_at: "2026-04-27T09:18:50Z",
    hypothesis: "feature flag search.semantic-rerank flip caused NPE on empty docs",
    confidence: 0.91,
    action: { command: "Disable feature flag search.semantic-rerank", blast: "low", threshold: 0.80 },
  },
  {
    id: "0193f4d3-3e66-7d44-cf02-40ad26019404",
    service: "payment",
    severity: "warn",
    triggered_at: "2026-04-28T22:46:30Z",
    hypothesis: "retry queue not draining; OOM in 90 min",
    confidence: 0.71,
    action: { command: "Roll largest payment pod; investigate dequeue path", blast: "medium", threshold: 0.90 },
  },
];

const BLAST_TONE: Record<Blast, "pos" | "warn" | "neg"> = { low: "pos", medium: "warn", high: "neg" };

const SEV_TONE: Record<Row["severity"], "neg" | "warn" | "neutral"> = {
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

export default function IncidentsPage() {
  const pages = ROWS.filter((r) => r.severity === "page");
  const others = ROWS.filter((r) => r.severity !== "page");
  const actionable = ROWS.filter((r) => r.confidence >= 0.85).length;

  return (
    <div className="flex flex-col gap-12">
      {/* HERO — editorial serif, anchored left. Single Fraunces moment. */}
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="eyebrow mb-3">Open incidents</p>
          <h1 className="font-serif text-3xl font-normal tracking-tight text-ink-0">
            {ROWS.length} incidents
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
            <dd className="mt-1 font-mono text-lg tabular-nums text-ink-0">demo</dd>
          </div>
        </dl>
      </header>

      {/* GROUP: pages — tight rhythm. */}
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

      {/* GROUP: warnings — quieter. */}
      {others.length > 0 && (
        <section>
          <h2 className="eyebrow mb-3">Warnings</h2>
          <ul className="divide-y divide-ink-7 border-y border-ink-7" role="list">
            {others.map((r) => (
              <IncidentRow key={r.id} row={r} />
            ))}
          </ul>
        </section>
      )}

      {ROWS.length === 0 && (
        <p className="max-w-prose text-ink-3">
          No alerts yet. Point your OpenTelemetry exporter at <code>/v1/traces</code> to start;
          the agent will assemble its first RCA on the next page.
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
