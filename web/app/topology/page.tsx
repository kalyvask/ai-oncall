// Topology — asymmetric two-column layout. Left column: nodes currently
// in alarm (the only thing the SRE cares about at 2am). Right column:
// healthy services, quiet roll-call. NO symmetric card grid (impeccable
// reflex-reject pattern).

import { Pill } from "@/components/Pill";

const NODES = [
  { service: "checkout", status: "warn" as const, deps: ["cart", "payment", "currency", "shipping"] },
  { service: "payment", status: "error" as const, deps: ["stripe"] },
  { service: "cart", status: "ok" as const, deps: ["cart-db"] },
  { service: "shipping", status: "ok" as const, deps: [] },
  { service: "currency", status: "ok" as const, deps: [] },
];

const STATUS_TONE = { ok: "pos", warn: "warn", error: "neg", unknown: "neutral" } as const;

export default function TopologyPage() {
  const broken = NODES.filter((n) => n.status === "error" || n.status === "warn");
  const healthy = NODES.filter((n) => n.status === "ok");

  return (
    <div className="flex flex-col gap-10">
      <header>
        <p className="eyebrow mb-3">System graph</p>
        <h1 className="text-2xl font-medium tracking-tight text-ink-0">Topology</h1>
        <p className="mt-2 max-w-prose text-sm text-ink-3">
          Live span-derived topology with 10-minute decay arrives once OTLP ingest is wired.
          Today this reads from <code>topology.yaml</code>.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-12 lg:grid-cols-[2fr_1fr]">
        {/* DOMINANT — what's broken now. */}
        <section>
          <h2 className="eyebrow mb-4">Affected · {broken.length}</h2>
          <ul className="flex flex-col" role="list">
            {broken.map((n) => (
              <li
                key={n.service}
                className="grid grid-cols-[auto_1fr_auto] items-baseline gap-5 border-t border-ink-7 py-4 first:border-t-0"
              >
                <Pill tone={STATUS_TONE[n.status]}>{n.status}</Pill>
                <div>
                  <code className="text-base text-ink-0">{n.service}</code>
                  {n.deps.length > 0 && (
                    <p className="mt-1 text-xs text-ink-3">
                      depends on{" "}
                      {n.deps.map((d, i) => (
                        <span key={d}>
                          <code className="text-ink-2">{d}</code>
                          {i < n.deps.length - 1 ? ", " : ""}
                        </span>
                      ))}
                    </p>
                  )}
                </div>
                <a href="/" className="text-xs text-acc-hi underline-offset-2 hover:underline">
                  open incident
                </a>
              </li>
            ))}
          </ul>
        </section>

        {/* QUIET — healthy roll-call. */}
        <section>
          <h2 className="eyebrow mb-4">Healthy · {healthy.length}</h2>
          <ul className="flex flex-col gap-2" role="list">
            {healthy.map((n) => (
              <li key={n.service} className="flex items-center justify-between text-sm text-ink-2">
                <code>{n.service}</code>
                <span className="text-xs text-ink-4">ok</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
