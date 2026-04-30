// Single bordered surface, NEVER nested. Used sparingly — most groupings
// should be expressed by spacing + a thin top divider rule, not by wrapping
// everything in a card (impeccable §Layout).
import { ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section
      className={`rounded-md border border-ink-7 bg-ink-8 p-6 ${className}`}
    >
      {children}
    </section>
  );
}
