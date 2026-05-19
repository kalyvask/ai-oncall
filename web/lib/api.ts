// FastAPI client. Every request carries X-Tenant-Id. The tenant is read from
// a `tenant` cookie on the client; on the server it falls back to the
// AI_ONCALL_DEFAULT_TENANT env var (or "demo").

function tenantFromBrowser(): string {
  if (typeof window === "undefined") return "";
  const match = document.cookie.split("; ").find((c) => c.startsWith("tenant="));
  return match ? match.slice("tenant=".length) : "";
}

export function currentTenant(): string {
  return tenantFromBrowser() || process.env.AI_ONCALL_DEFAULT_TENANT || "demo";
}

const BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Tenant-Id", currentTenant());
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

export type IncidentRow = {
  report_id: string;
  alert_id: string;
  service: string;
  root_cause_service: string;
  root_cause_class: string | null;
  top_confidence: number;
  abstained: boolean;
  trust_tier: "local" | "aggregated" | "verified";
  created_at: string;
};

export type ToolCall = {
  tool: string;
  input: Record<string, unknown>;
  result_summary: string;
  result_size: number;
  duration_ms: number;
};

export type StagedAction = {
  kind: string;
  service: string;
  tier: "recommend" | "propose" | "auto";
  command?: string;
  blast_radius?: "low" | "medium" | "high";
  approval_threshold?: number;
};

export type Hypothesis = {
  statement: string;
  confidence: number;
  root_cause_service: string;
  reasoning?: string;
  evidence?: Array<{ claim: string; source: string }>;
  recommended_action?: string;
  staged_action?: StagedAction | null;
};

export type IncidentDetail = {
  report_id: string;
  tenant_id: string;
  alert: {
    alert_id: string;
    tenant_id: string;
    fired_at: string;
    source: string;
    severity: "page" | "warn" | "info";
    service: string;
    title: string;
    signal?: Record<string, unknown>;
    labels?: Record<string, string>;
  };
  generated_at: string;
  model?: { provider: string; id: string };
  investigation?: {
    tool_calls: ToolCall[];
    tokens_in?: number;
    tokens_out?: number;
  };
  hypotheses: Hypothesis[];
};

export type IncidentTrace = {
  report_id: string;
  alert: IncidentDetail["alert"];
  tool_calls: ToolCall[];
  hypotheses: Hypothesis[];
};

export type JobRecord = {
  job_id: string;
  kind: "rca" | "slack_post";
  status: "pending" | "running" | "done" | "failed";
  attempts: number;
  max_attempts: number;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type Reaction = "thumbs_up" | "thumbs_down" | "wrong_root_cause";

export const api = {
  health: () => req<{ ok: boolean }>("/health"),
  ready: () => req<{ ok: boolean; worker: boolean; jobs_db: boolean }>("/ready"),
  topology: () => req<TopologySnapshot>("/topology"),
  incidents: (limit = 25) =>
    req<{ tenant_id: string; items: IncidentRow[] }>(`/incidents?limit=${limit}`),
  incident: (id: string) => req<IncidentDetail>(`/incidents/${id}`),
  incidentTrace: (id: string) => req<IncidentTrace>(`/incidents/${id}/trace`),
  job: (jobId: string) => req<JobRecord>(`/jobs/${jobId}`),
  feedback: (report_id: string, reaction: Reaction, correction?: string) =>
    req<{ ok: boolean }>("/feedback", {
      method: "POST",
      body: JSON.stringify({ report_id, reaction, correction }),
    }),
  approveAction: (report_id: string) =>
    req<{ ok: boolean }>(`/actions/${report_id}/approve`, { method: "POST" }),
};

export const TENANT_ID = currentTenant();
