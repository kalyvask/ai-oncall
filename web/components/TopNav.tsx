import Link from "next/link";

const LINKS = [
  { href: "/", label: "Incidents" },
  { href: "/topology", label: "Topology" },
  { href: "/runbooks", label: "Runbooks" },
  { href: "/settings", label: "Settings" },
];

// Asymmetric: brand left-anchored, links pushed right against a tenant chip.
// No center-spaced nav (one of design-for-ai's "everything centered" tells).
export function TopNav() {
  return (
    <nav
      aria-label="primary"
      className="sticky top-0 z-10 border-b border-ink-7 bg-ink-9/85 backdrop-blur"
    >
      <div className="mx-auto flex max-w-[1180px] items-center gap-8 px-6 py-4 sm:px-10">
        <Link
          href="/"
          className="text-base font-semibold tracking-tight text-ink-0 transition-colors duration-fast hover:text-acc-hi"
        >
          ai-oncall
        </Link>

        <ul className="flex items-center gap-6 text-sm">
          {LINKS.map((l) => (
            <li key={l.href}>
              <Link
                href={l.href}
                className="text-ink-3 transition-colors duration-fast hover:text-ink-0"
              >
                {l.label}
              </Link>
            </li>
          ))}
        </ul>

        {/* Tenant pip — far right, the only chrome in the bar. */}
        <span className="ml-auto inline-flex items-center gap-2 rounded-sm border border-ink-7 px-3 py-1 text-xs text-ink-2">
          <span className="h-1.5 w-1.5 rounded-full bg-acc" aria-hidden />
          tenant <code className="text-ink-1">demo</code>
        </span>
      </div>
    </nav>
  );
}
