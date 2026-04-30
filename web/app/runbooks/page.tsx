// Runbooks — flat list, no card-per-runbook (no identical card grid).
// CodeMirror 6 editor lands once the API exposes /runbooks; today this is
// a directory view.

const RUNBOOKS = [
  {
    service: "checkout",
    path: "runbooks/checkout.md",
    excerpt:
      "checkout retries on payment failure with exponential backoff up to 3 attempts before returning a 502, so payment errors show up as checkout latency, not checkout 5xx.",
    failure_modes: 3,
  },
  {
    service: "payment",
    path: "runbooks/payment.md",
    excerpt:
      "Roll back the most recent payment deploy if the alert window contains a deploy in the last 30 minutes. Default rollback is reversible inside the deploy window.",
    failure_modes: 2,
  },
];

export default function RunbooksPage() {
  return (
    <div className="flex flex-col gap-10">
      <header>
        <p className="eyebrow mb-3">Markdown · {RUNBOOKS.length} services</p>
        <h1 className="text-2xl font-medium tracking-tight text-ink-0">Runbooks</h1>
      </header>

      <ul className="flex flex-col" role="list">
        {RUNBOOKS.map((r) => (
          <li key={r.path} className="border-t border-ink-7 py-6 first:border-t-0">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_auto] md:items-baseline">
              <div>
                <code className="text-base text-ink-0">{r.service}</code>
                <p className="mt-1 text-xs text-ink-4">{r.path}</p>
                <p className="mt-3 max-w-prose text-sm text-ink-2">{r.excerpt}</p>
              </div>
              <div className="text-right text-xs text-ink-3">
                <span className="font-mono tabular-nums">{r.failure_modes}</span> failure modes
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
