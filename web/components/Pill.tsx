import { ReactNode } from "react";

// Status communicated by COLOR + SHAPE (a leading dot pip), not color alone.
// Square corners — pills are visually loud enough; no need for the ubiquitous
// rounded-full SaaS chip. This keeps the surface from drifting into the
// "AI default" badge look.
type Tone = "neutral" | "pos" | "warn" | "neg" | "acc";

const TONE: Record<Tone, { bg: string; fg: string; dot: string }> = {
  neutral: { bg: "bg-ink-7",                      fg: "text-ink-1",  dot: "bg-ink-3" },
  pos:     { bg: "bg-[oklch(72%_0.14_150_/_18%)]", fg: "text-pos",    dot: "bg-pos" },
  warn:    { bg: "bg-[oklch(80%_0.16_50_/_20%)]",  fg: "text-warn",   dot: "bg-warn" },
  neg:     { bg: "bg-[oklch(64%_0.20_22_/_20%)]",  fg: "text-neg",    dot: "bg-neg" },
  acc:     { bg: "bg-acc-bg",                      fg: "text-acc-hi", dot: "bg-acc" },
};

export function Pill({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  const t = TONE[tone];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-sm px-2 py-0.5 text-xs font-medium ${t.bg} ${t.fg}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${t.dot}`} aria-hidden />
      {children}
    </span>
  );
}
