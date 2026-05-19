// Per-million-token rates mirrored from ai_oncall/llm/registry.py.
// Kept in sync by convention; the canonical source of truth is the Python
// CATALOG. Used only for UI-side cost display.

export const CATALOG_RATES: Record<string, { in: number; out: number }> = {
  "claude-haiku-4-5-20251001": { in: 1.0, out: 5.0 },
  "claude-sonnet-4-6": { in: 3.0, out: 15.0 },
  "claude-opus-4-7": { in: 15.0, out: 75.0 },
};
