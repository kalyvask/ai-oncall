// Atom for every metric. Eyebrow + tabular value + optional delta.
// Tabular numerics keep columns aligned when values change in place.
export function Statline({ label, value, delta }: { label: string; value: string; delta?: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="eyebrow">{label}</span>
      <span className="font-mono text-lg tabular-nums text-ink-0">{value}</span>
      {delta && <span className="text-xs text-ink-3">{delta}</span>}
    </div>
  );
}
