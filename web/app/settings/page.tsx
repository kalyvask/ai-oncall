// Settings — single form, no card-per-field. Stripes separated by hairlines.
// Tenant switcher, model selector, cost ceiling, telemetry-store driver.

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-10">
      <header>
        <p className="eyebrow mb-3">Tenant · model · budget</p>
        <h1 className="text-2xl font-medium tracking-tight text-ink-0">Settings</h1>
      </header>

      <form className="flex flex-col">
        <Field
          label="Tenant"
          help="Sent as X-Tenant-Id on every request. BRIEF.md §8."
        >
          <input
            name="tenant"
            defaultValue="demo"
            className="w-full max-w-xs rounded-md border border-ink-7 bg-ink-9 px-3 py-2 font-mono text-ink-0 focus:border-acc focus:outline-none"
          />
        </Field>

        <Field
          label="Model"
          help="claude-haiku is the default. claude-opus is opt-in via AI_ONCALL_RCA_MODEL."
        >
          <select
            defaultValue="claude-haiku"
            className="w-full max-w-xs rounded-md border border-ink-7 bg-ink-9 px-3 py-2 text-ink-0 focus:border-acc focus:outline-none"
          >
            <option value="claude-haiku">claude-haiku-4-5 (default)</option>
            <option value="claude-sonnet">claude-sonnet-4-6</option>
            <option value="claude-opus">claude-opus-4-7 (opt-in)</option>
            <option value="mock">mock-deterministic (eval)</option>
          </select>
        </Field>

        <Field
          label="Cost ceiling"
          help="Hard upper $2.00. Default $0.50 per RCA."
        >
          <div className="flex items-center gap-3">
            <input
              type="number"
              step="0.01"
              min="0"
              max="2"
              defaultValue="0.50"
              className="w-32 rounded-md border border-ink-7 bg-ink-9 px-3 py-2 font-mono tabular-nums text-ink-0 focus:border-acc focus:outline-none"
            />
            <span className="text-sm text-ink-3">USD</span>
          </div>
        </Field>

        <Field
          label="Telemetry store"
          help="Snowflake driver is stubbed in v1; SQLite is the dev default."
        >
          <select
            defaultValue="sqlite"
            className="w-full max-w-xs rounded-md border border-ink-7 bg-ink-9 px-3 py-2 text-ink-0 focus:border-acc focus:outline-none"
          >
            <option value="sqlite">SQLite (dev)</option>
            <option value="duckdb">DuckDB (single-node prod)</option>
            <option value="snowflake">Snowflake (multi-tenant prod, stub)</option>
          </select>
        </Field>

        <div className="flex justify-end pt-8">
          <button
            type="button"
            className="rounded-md bg-acc px-5 py-2 text-sm font-semibold text-ink-9 transition-colors duration-fast hover:bg-acc-hi"
          >
            Save changes
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-3 border-t border-ink-7 py-6 first:border-t-0 md:grid-cols-[14rem_1fr]">
      <div>
        <p className="text-sm font-medium text-ink-0">{label}</p>
        {help && <p className="mt-1 max-w-prose text-xs text-ink-4">{help}</p>}
      </div>
      <div>{children}</div>
    </div>
  );
}
