// FastAPI client. Every request carries X-Tenant-Id (BRIEF.md §8 — multi-
// tenancy without auth). The tenant is read from a `tenant` cookie / header
// pair that the settings page sets; default is `demo` for local dev.

const TENANT = (typeof window !== "undefined"
  ? document.cookie.split("; ").find((c) => c.startsWith("tenant="))?.slice("tenant=".length)
  : null) || "demo";

const BASE = process.env.NEXT_PUBLIC_API_BASE || "/api";

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Tenant-Id", TENANT);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const r = await fetch(BASE + path, { ...init, headers, cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${await r.text()}`);
  return (await r.json()) as T;
}

export type TopologyNode = { service: string; status: "ok" | "warn" | "error" | "unknown" };
export type TopologyEdge = { from: string; to: string };
export type TopologySnapshot = {
  tenant_id: string;
  captured_at: string;
  nodes: TopologyNode[];
  edges: TopologyEdge[];
};

export const api = {
  health: () => req<{ ok: boolean }>("/health"),
  topology: () => req<TopologySnapshot>("/topology"),
  // Endpoints for the incidents list and detail land in BRIEF.md step 8 backend pass.
  // Until then, the UI reads a fixture; see app/page.tsx.
};

export const TENANT_ID = TENANT;
